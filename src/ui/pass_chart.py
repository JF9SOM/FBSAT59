"""
Graphical pass prediction display widget

PassChartView  — elevation vs time chart using PySide6 + QtCharts (with time range selector)
pass_quality() — shared utility that returns a quality rank from a maximum elevation
elevation_points() — generates a sine-approximated elevation point sequence from AOS/TCA/LOS

Quality ranks and colours:
    excellent (>=60°): green  #2ecc71
    good      (>=30°): blue   #3498db
    fair      (>=10°): yellow #f1c40f
    low       (< 10°): grey   #95a5a6
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from PySide6.QtCharts import (
    QChart,
    QChartView,
    QDateTimeAxis,
    QLineSeries,
    QSplineSeries,
    QValueAxis,
)
from PySide6.QtCore import QDateTime, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QGraphicsTextItem,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from i18n import _

if TYPE_CHECKING:
    from core.engine import PassInfo
    from ui.pass_panel import GroupPassResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

QUALITY_COLORS: dict[str, QColor] = {
    "excellent": QColor("#2ecc71"),
    "good": QColor("#3498db"),
    "fair": QColor("#f1c40f"),
    "low": QColor("#95a5a6"),
}

_ELEVATION_SAMPLE_POINTS = 20


def _range_options() -> tuple[tuple[str, float], ...]:
    """Built lazily (not as a module-level constant) so labels pick up the
    current UI language each time a chart view is constructed."""
    return (
        (_("Next 1 hour"), 1.0),
        (_("Next 4 hours"), 4.0),
        (_("Next 8 hours"), 8.0),
        (_("Next 12 hours"), 12.0),
        (_("Next 24 hours"), 24.0),
    )


# ---------------------------------------------------------------------------
# Utilities (pure functions with no UI dependency)
# ---------------------------------------------------------------------------


def pass_quality(max_elevation_deg: float) -> str:
    """
    Return the pass quality rank string from a maximum elevation.

    Args:
        max_elevation_deg: maximum elevation of the pass (degrees)

    Returns:
        "excellent" (>=60) / "good" (>=30) / "fair" (>=10) / "low" (<10)
    """
    if max_elevation_deg >= 60.0:
        return "excellent"
    if max_elevation_deg >= 30.0:
        return "good"
    if max_elevation_deg >= 10.0:
        return "fair"
    return "low"


def elevation_points(
    aos: datetime,
    tca: datetime,
    los: datetime,
    max_elevation_deg: float,
    n_points: int = _ELEVATION_SAMPLE_POINTS,
) -> list[tuple[float, float]]:
    """
    Generate a sine-approximated elevation point sequence from AOS, TCA, LOS, and max elevation.

    The AOS->TCA segment uses sin(pi*t/2) and the TCA->LOS segment uses cos(pi*t/2).
    x values are Unix timestamps [ms]; y values are elevation [degrees].

    Args:
        aos: satellite rise time (UTC)
        tca: time of maximum elevation (UTC)
        los: satellite set time (UTC)
        max_elevation_deg: maximum elevation (degrees)
        n_points: number of samples for each of the AOS->TCA and TCA->LOS segments

    Returns:
        list of (timestamp_ms, elevation_deg) tuples
    """
    if aos.tzinfo is None:
        aos = aos.replace(tzinfo=UTC)
    if tca.tzinfo is None:
        tca = tca.replace(tzinfo=UTC)
    if los.tzinfo is None:
        los = los.replace(tzinfo=UTC)

    aos_ms = aos.timestamp() * 1000.0
    tca_ms = tca.timestamp() * 1000.0
    los_ms = los.timestamp() * 1000.0
    points: list[tuple[float, float]] = []

    # AOS -> TCA (sin curve rising)
    for i in range(n_points):
        t = i / n_points
        el = max_elevation_deg * math.sin(math.pi * t / 2.0)
        ms = aos_ms + t * (tca_ms - aos_ms)
        points.append((ms, el))

    # TCA (peak)
    points.append((tca_ms, max_elevation_deg))

    # TCA -> LOS (cos curve descending)
    for i in range(1, n_points + 1):
        t = i / n_points
        el = max_elevation_deg * math.cos(math.pi * t / 2.0)
        ms = tca_ms + t * (los_ms - tca_ms)
        points.append((ms, el))

    return points


# ---------------------------------------------------------------------------
# PassChartView widget
# ---------------------------------------------------------------------------


class PassChartView(QWidget):
    """
    PySide6 widget that displays a satellite pass elevation vs time chart.

    Has a time-range dropdown at the top and draws only passes within the selected range.
    Peak elevation labels are placed directly in the scene as QGraphicsTextItems.

    Usage::

        chart = PassChartView()
        chart.set_passes(passes, sat_name="ISS (ZARYA)")
        layout.addWidget(chart)

    Signals:
        pass_clicked(PassInfo): emitted when a pass curve is clicked
        range_changed(float):   emits the selected number of hours when the time range changes
    """

    pass_clicked: Signal = Signal(object)  # emit(PassInfo)
    range_changed: Signal = Signal(float)  # emit(hours)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._passes: list[PassInfo] = []
        self._sat_name: str = ""
        self._series_to_pass: dict[QSplineSeries, PassInfo] = {}
        self._overlay: list[tuple[QSplineSeries, float, QColor]] = []
        self._peak_label_items: list[QGraphicsTextItem] = []
        self._use_utc: bool = True
        self._setup_ui()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # Time range dropdown
        header = QWidget()
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(6, 2, 6, 2)
        h_layout.addWidget(QLabel("Range:"))
        self._range_combo = QComboBox()
        range_options = _range_options()
        for label, _hours in range_options:
            self._range_combo.addItem(label)
        self._range_combo.setCurrentIndex(len(range_options) - 1)  # default to "Next 24 hours"
        self._range_combo.currentIndexChanged.connect(self._on_range_changed)
        h_layout.addWidget(self._range_combo)
        self._refresh_btn = QToolButton()
        self._refresh_btn.setText("\U0001f504")  # 🔄
        self._refresh_btn.setToolTip(_("Refresh"))
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        h_layout.addWidget(self._refresh_btn)
        h_layout.addStretch()
        layout.addWidget(header)

        # Chart view (animation disabled so series render immediately)
        self._chart = QChart()
        self._chart.setAnimationOptions(QChart.AnimationOption.NoAnimation)
        self._chart.legend().setVisible(True)
        self._chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)

        self._chart_view = QChartView(self._chart)
        self._chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._chart_view.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        layout.addWidget(self._chart_view)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def set_passes(self, passes: list[PassInfo], sat_name: str = "") -> None:
        """
        Set the pass list to display and redraw the chart.

        Args:
            passes:   list of PassInfo objects (up to 24 hours recommended)
            sat_name: satellite name used in the chart title
        """
        self._passes = passes
        self._sat_name = sat_name
        self._rebuild()

    def set_use_utc(self, use_utc: bool) -> None:
        """Switch the time axis between UTC and local time display.

        Args:
            use_utc: True → axis labels and title show UTC;
                     False → axis labels show system local time.
        """
        if use_utc == self._use_utc:
            return
        self._use_utc = use_utc
        self._rebuild()

    def clear(self) -> None:
        """Clear the chart."""
        self._passes = []
        self._sat_name = ""
        self._series_to_pass = {}
        self._overlay = []
        self._clear_peak_labels()
        self._chart.removeAllSeries()
        for axis in self._chart.axes():
            self._chart.removeAxis(axis)
        self._chart.setTitle("")

    # ------------------------------------------------------------------ #
    # Chart construction
    # ------------------------------------------------------------------ #

    def _on_range_changed(self, _idx: int) -> None:
        hours = self._selected_hours()
        self.range_changed.emit(hours)
        self._rebuild()

    def _on_refresh_clicked(self) -> None:
        """Re-fetch predictions for the currently selected range (same path as
        changing the Range dropdown, without actually changing the selection)."""
        self.range_changed.emit(self._selected_hours())

    def _selected_hours(self) -> float:
        idx = self._range_combo.currentIndex()
        return _range_options()[idx][1]

    def _rebuild(self) -> None:
        """Rebuild the chart using passes within the selected time range."""
        # Remove existing peak labels from the scene before deleting series
        self._clear_peak_labels()
        self._overlay = []
        self._chart.removeAllSeries()
        for axis in self._chart.axes():
            self._chart.removeAxis(axis)
        self._series_to_pass = {}

        hours = self._selected_hours()
        now = datetime.now(UTC)
        cutoff = now + timedelta(hours=hours)
        filtered = [p for p in self._passes if p.los >= now and p.aos <= cutoff]

        if not filtered:
            self._chart.setTitle("No passes in range")
            return

        title = f"{self._sat_name} Pass Prediction" if self._sat_name else "Pass Prediction"
        self._chart.setTitle(title)

        dt_axis = self._make_time_axis()
        el_axis = self._make_elevation_axis()
        self._chart.addAxis(dt_axis, Qt.AlignmentFlag.AlignBottom)
        self._chart.addAxis(el_axis, Qt.AlignmentFlag.AlignLeft)

        overlay: list[tuple[QSplineSeries, float, QColor]] = []
        for p in filtered:
            series = self._build_pass_series(p)
            self._chart.addSeries(series)
            series.attachAxis(dt_axis)
            series.attachAxis(el_axis)
            self._series_to_pass[series] = p
            series.clicked.connect(self._on_series_clicked)

            quality = pass_quality(p.max_elevation_deg)
            overlay.append((series, p.max_elevation_deg, QUALITY_COLORS[quality]))

        # Current time line
        t_end = max(p.los for p in filtered)
        if now <= t_end:
            now_series = self._build_now_line(now)
            self._chart.addSeries(now_series)
            now_series.attachAxis(dt_axis)
            now_series.attachAxis(el_axis)

        # Set the range after all series have been added (attachAxis overwrites auto-range)
        dt_axis.setRange(
            self._epoch_ms_to_qdatetime(now.timestamp() * 1000),
            self._epoch_ms_to_qdatetime(cutoff.timestamp() * 1000),
        )
        el_axis.setRange(0.0, 90.0)

        self._overlay = overlay

        # Place labels in the scene 150 ms after Qt Charts finishes layout calculation
        # (mapToPosition() returns incorrect coordinates before layout is finalised)
        QTimer.singleShot(150, self._add_peak_labels)

    def _clear_peak_labels(self) -> None:
        """Remove all peak-label QGraphicsTextItems from the scene."""
        scene = self._chart.scene()
        if scene is not None:
            for item in self._peak_label_items:
                scene.removeItem(item)
        self._peak_label_items.clear()

    def showEvent(self, event: object) -> None:  # noqa: ANN001
        """(Re)place labels when the widget becomes visible (e.g. on tab switch).

        The chart's drawing mapping is not yet finalised immediately after showEvent,
        so _add_peak_labels() is called after a short delay.
        """
        super().showEvent(event)  # type: ignore[arg-type]
        if self._overlay:
            QTimer.singleShot(50, self._add_peak_labels)

    def _add_peak_labels(self, retry: int = 0) -> None:
        """Place elevation labels at each pass peak as QGraphicsTextItems in the scene.

        Root cause and mitigation:
          - Inactive QTabWidget tabs have isVisible()=False.
            In that state plotArea() returns a plausible size, but the chart's
            internal data-to-pixel mapping is uninitialised, so mapToPosition()
            returns values near (0,0) and all labels cluster in the top-left corner.
          - Check isVisible() and do nothing if hidden (showEvent will trigger later).
          - mapToPosition() returns chart-local coordinates, so convert to scene
            coordinates with mapToScene() before calling setPos().
          - If plotArea is very small, retry up to 5 times at 150 ms intervals.
        """
        self._clear_peak_labels()

        # Do not place labels on a hidden chart (showEvent will trigger them)
        if not self.isVisible():
            return

        plot_area = self._chart.plotArea()
        if plot_area.width() <= 1 or plot_area.height() <= 1:
            if retry < 5:
                QTimer.singleShot(150, lambda: self._add_peak_labels(retry + 1))
            return

        scene = self._chart.scene()
        if scene is None:
            return

        label_font = QFont()
        label_font.setPointSize(8)
        label_font.setBold(True)

        for series, max_el, color in self._overlay:
            # Find the data point with the highest elevation
            best: QPointF | None = None
            for i in range(series.count()):
                pt = series.at(i)
                if best is None or pt.y() > best.y():
                    best = QPointF(pt.x(), pt.y())
            if best is None:
                continue

            try:
                # mapToPosition() returns chart-local coordinates
                chart_pt = self._chart.mapToPosition(best, series)
                if not plot_area.contains(chart_pt):
                    continue

                # Convert chart-local coordinates to scene coordinates before placing
                scene_pos = self._chart.mapToScene(chart_pt)

                lbl = f"{max_el:.0f}°"
                item = QGraphicsTextItem(lbl)
                item.setDefaultTextColor(color)
                item.setFont(label_font)
                bw = item.boundingRect().width()
                item.setPos(scene_pos.x() - bw / 2.0, scene_pos.y() - 18.0)
                scene.addItem(item)
                self._peak_label_items.append(item)
            except Exception:  # noqa: BLE001
                pass

    def _epoch_ms_to_qdatetime(self, epoch_ms: float) -> QDateTime:
        """Convert epoch milliseconds to a QDateTime in the active display timezone."""
        if self._use_utc:
            return QDateTime.fromMSecsSinceEpoch(int(epoch_ms), Qt.TimeSpec.UTC)
        return QDateTime.fromMSecsSinceEpoch(int(epoch_ms))

    def _make_time_axis(self) -> QDateTimeAxis:
        axis = QDateTimeAxis()
        axis.setFormat("HH:mm")
        axis.setTitleText(_("Time (UTC)") if self._use_utc else _("Time (Local)"))
        axis.setTickCount(7)
        return axis

    def _make_elevation_axis(self) -> QValueAxis:
        axis = QValueAxis()
        axis.setRange(0.0, 90.0)
        axis.setTitleText(_("Elevation (°)"))
        axis.setTickCount(10)
        axis.setLabelFormat("%d")
        return axis

    def _build_pass_series(self, p: PassInfo) -> QSplineSeries:
        quality = pass_quality(p.max_elevation_deg)
        color = QUALITY_COLORS[quality]

        series = QSplineSeries()
        series.setName(f"max {p.max_elevation_deg:.0f}° ({quality})")

        pen = QPen(color)
        pen.setWidth(2)
        series.setPen(pen)

        pts = elevation_points(p.aos, p.tca, p.los, p.max_elevation_deg)
        for ms, el in pts:
            series.append(QPointF(ms, el))

        return series

    def _build_now_line(self, now: datetime) -> QLineSeries:
        now_ms = now.timestamp() * 1000.0
        series = QLineSeries()
        series.setName(_("Now"))
        pen = QPen(QColor("#e74c3c"))
        pen.setWidth(2)
        pen.setStyle(Qt.PenStyle.DashLine)
        series.setPen(pen)
        series.append(QPointF(now_ms, 0.0))
        series.append(QPointF(now_ms, 90.0))
        return series

    # ------------------------------------------------------------------ #
    # Signal handlers
    # ------------------------------------------------------------------ #

    def _on_series_clicked(self, point: QPointF) -> None:
        sender = self.sender()
        if isinstance(sender, QSplineSeries) and sender in self._series_to_pass:
            self.pass_clicked.emit(self._series_to_pass[sender])


# ---------------------------------------------------------------------------
# GroupPassChartView — multi-satellite pass chart (satellite-colour coded)
# ---------------------------------------------------------------------------

# Palette of distinct colours for per-satellite colouring (cycles if >N sats)
_GROUP_PALETTE: tuple[str, ...] = (
    "#e74c3c",  # red
    "#3498db",  # blue
    "#2ecc71",  # green
    "#f39c12",  # orange
    "#9b59b6",  # purple
    "#1abc9c",  # teal
    "#e67e22",  # dark orange
    "#2980b9",  # dark blue
    "#27ae60",  # dark green
    "#8e44ad",  # dark purple
    "#16a085",  # dark teal
    "#d35400",  # burnt orange
)


class GroupPassChartView(QWidget):
    """
    Multi-satellite pass elevation chart for Group search results.

    Each satellite gets a distinct colour from the palette (cycles if >12 sats).
    Satellite names are shown as tooltips on hover rather than permanent legend entries.

    Usage::

        chart = GroupPassChartView()
        chart.set_results(group_results)   # list[GroupPassResult]
        layout.addWidget(chart)

    Signals:
        range_changed(float): emits the selected number of hours when the range
                               dropdown changes, so the owner can re-run the Group
                               search for the new window (the chart itself only
                               re-filters the results it already has — widening
                               the range past what was last searched would
                               otherwise show nothing new).
    """

    range_changed: Signal = Signal(float)  # emit(hours)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._results: list[GroupPassResult] = []
        self._use_utc: bool = True
        self._overlay: list[tuple[QSplineSeries, float, QColor, str]] = []
        self._peak_label_items: list[QGraphicsTextItem] = []
        self._series_to_info: dict[QSplineSeries, tuple[str, float]] = {}
        self._setup_ui()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        header = QWidget()
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(6, 2, 6, 2)
        h_layout.addWidget(QLabel("Range:"))
        self._range_combo = QComboBox()
        range_options = _range_options()
        for label, _hours in range_options:
            self._range_combo.addItem(label)
        # Default to "Next 4 hours" — the Group tab's own auto-search window is
        # 4h, so the chart should show the same span by default. (Unlike
        # PassChartView's Target chart, which stays at 24h/last index.) Found
        # by value rather than a hardcoded index so inserting/reordering
        # options in _range_options() can't silently change this default.
        default_idx = next(i for i, (_label, hours) in enumerate(range_options) if hours == 4.0)
        self._range_combo.setCurrentIndex(default_idx)
        self._range_combo.currentIndexChanged.connect(self._on_range_changed)
        h_layout.addWidget(self._range_combo)
        self._refresh_btn = QToolButton()
        self._refresh_btn.setText("\U0001f504")  # 🔄
        self._refresh_btn.setToolTip(_("Refresh"))
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        h_layout.addWidget(self._refresh_btn)
        h_layout.addStretch()
        layout.addWidget(header)

        self._chart = QChart()
        self._chart.setAnimationOptions(QChart.AnimationOption.NoAnimation)
        self._chart.legend().setVisible(False)  # hidden — names shown as tooltips

        self._chart_view = QChartView(self._chart)
        self._chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._chart_view.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        # Enable mouse tracking so hovered() signal fires
        self._chart_view.setMouseTracking(True)
        layout.addWidget(self._chart_view)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def set_results(self, results: list[GroupPassResult]) -> None:
        """Set group search results and redraw.

        Args:
            results: list[GroupPassResult] from PassPanel.group_results_ready
        """
        self._results = results
        self._rebuild()

    def current_range_hours(self) -> float:
        """Return the currently selected Range dropdown value, in hours.

        Lets an auto-search triggered elsewhere (e.g. a filter change) reuse
        whichever window the user last selected on this chart, instead of
        always resetting to the 4h default.
        """
        return self._selected_hours()

    def set_use_utc(self, use_utc: bool) -> None:
        """Switch time axis between UTC and local time."""
        if use_utc == self._use_utc:
            return
        self._use_utc = use_utc
        self._rebuild()

    def clear(self) -> None:
        """Clear all chart content."""
        self._results = []
        self._clear_peak_labels()
        self._chart.removeAllSeries()
        for axis in self._chart.axes():
            self._chart.removeAxis(axis)
        self._chart.setTitle("")
        self._series_to_info = {}
        self._overlay = []

    # ------------------------------------------------------------------ #
    # Chart construction
    # ------------------------------------------------------------------ #

    def _selected_hours(self) -> float:
        return _range_options()[self._range_combo.currentIndex()][1]

    def _on_range_changed(self, _idx: int) -> None:
        hours = self._selected_hours()
        self.range_changed.emit(hours)
        self._rebuild()

    def _on_refresh_clicked(self) -> None:
        """Re-run the Group search for the currently selected range (same path
        as changing the Range dropdown, without actually changing the selection)."""
        self.range_changed.emit(self._selected_hours())

    def _rebuild(self) -> None:
        """Rebuild the chart from the current result list."""
        self._clear_peak_labels()
        self._chart.removeAllSeries()
        for axis in self._chart.axes():
            self._chart.removeAxis(axis)
        self._series_to_info = {}
        self._overlay = []

        if not self._results:
            self._chart.setTitle("No group results")
            return

        hours = self._selected_hours()
        now = datetime.now(UTC)
        cutoff = now + timedelta(hours=hours)

        # Filter to passes within range
        filtered = [
            r for r in self._results if r.pass_info.los >= now and r.pass_info.aos <= cutoff
        ]
        if not filtered:
            self._chart.setTitle("No passes in range")
            return

        self._chart.setTitle("Group Pass Prediction")

        dt_axis = self._make_time_axis()
        el_axis = self._make_elevation_axis()
        self._chart.addAxis(dt_axis, Qt.AlignmentFlag.AlignBottom)
        self._chart.addAxis(el_axis, Qt.AlignmentFlag.AlignLeft)

        # Assign colours per satellite name (stable mapping within this render)
        sat_color: dict[str, QColor] = {}
        palette_idx = 0
        overlay: list[tuple[QSplineSeries, float, QColor, str]] = []

        for r in filtered:
            sat_name: str = r.sat_name
            p = r.pass_info

            if sat_name not in sat_color:
                sat_color[sat_name] = QColor(_GROUP_PALETTE[palette_idx % len(_GROUP_PALETTE)])
                palette_idx += 1

            color = sat_color[sat_name]
            series = QSplineSeries()
            series.setName(sat_name)

            pen = QPen(color)
            pen.setWidth(2)
            series.setPen(pen)

            pts = elevation_points(p.aos, p.tca, p.los, p.max_elevation_deg)
            for ms, el in pts:
                series.append(QPointF(ms, el))

            self._chart.addSeries(series)
            series.attachAxis(dt_axis)
            series.attachAxis(el_axis)

            # Tooltip on hover
            series.hovered.connect(self._on_series_hovered)
            self._series_to_info[series] = (sat_name, p.max_elevation_deg)

            overlay.append((series, p.max_elevation_deg, color, sat_name))

        # Current-time line
        t_end = max(r.pass_info.los for r in filtered)
        if now <= t_end:
            now_series = self._build_now_line(now)
            self._chart.addSeries(now_series)
            now_series.attachAxis(dt_axis)
            now_series.attachAxis(el_axis)

        dt_axis.setRange(
            self._epoch_ms_to_qdatetime(now.timestamp() * 1000),
            self._epoch_ms_to_qdatetime(cutoff.timestamp() * 1000),
        )
        el_axis.setRange(0.0, 90.0)

        self._overlay = overlay
        QTimer.singleShot(150, self._add_peak_labels)

    def _clear_peak_labels(self) -> None:
        scene = self._chart.scene()
        if scene is not None:
            for item in self._peak_label_items:
                scene.removeItem(item)
        self._peak_label_items.clear()

    def showEvent(self, event: object) -> None:  # noqa: ANN001
        super().showEvent(event)  # type: ignore[arg-type]
        if self._overlay:
            QTimer.singleShot(50, self._add_peak_labels)

    def _add_peak_labels(self, retry: int = 0) -> None:
        """Place satellite-name labels at each pass peak."""
        self._clear_peak_labels()
        if not self.isVisible():
            return

        plot_area = self._chart.plotArea()
        if plot_area.width() <= 1 or plot_area.height() <= 1:
            if retry < 5:
                QTimer.singleShot(150, lambda: self._add_peak_labels(retry + 1))
            return

        scene = self._chart.scene()
        if scene is None:
            return

        label_font = QFont()
        label_font.setPointSize(7)

        for series, max_el, color, _sat_name in self._overlay:
            best: QPointF | None = None
            for i in range(series.count()):
                pt = series.at(i)
                if best is None or pt.y() > best.y():
                    best = QPointF(pt.x(), pt.y())
            if best is None:
                continue

            try:
                chart_pt = self._chart.mapToPosition(best, series)
                if not plot_area.contains(chart_pt):
                    continue
                scene_pos = self._chart.mapToScene(chart_pt)

                lbl = f"{max_el:.0f}°"
                item = QGraphicsTextItem(lbl)
                item.setDefaultTextColor(color)
                item.setFont(label_font)
                bw = item.boundingRect().width()
                item.setPos(scene_pos.x() - bw / 2.0, scene_pos.y() - 16.0)
                scene.addItem(item)
                self._peak_label_items.append(item)
            except Exception:  # noqa: BLE001
                pass

    def _epoch_ms_to_qdatetime(self, epoch_ms: float) -> QDateTime:
        if self._use_utc:
            return QDateTime.fromMSecsSinceEpoch(int(epoch_ms), Qt.TimeSpec.UTC)
        return QDateTime.fromMSecsSinceEpoch(int(epoch_ms))

    def _make_time_axis(self) -> QDateTimeAxis:
        axis = QDateTimeAxis()
        axis.setFormat("HH:mm")
        axis.setTitleText(_("Time (UTC)") if self._use_utc else _("Time (Local)"))
        axis.setTickCount(7)
        return axis

    def _make_elevation_axis(self) -> QValueAxis:
        axis = QValueAxis()
        axis.setRange(0.0, 90.0)
        axis.setTitleText(_("Elevation (°)"))
        axis.setTickCount(10)
        axis.setLabelFormat("%d")
        return axis

    def _build_now_line(self, now: datetime) -> QLineSeries:
        now_ms = now.timestamp() * 1000.0
        series = QLineSeries()
        series.setName(_("Now"))
        pen = QPen(QColor("#e74c3c"))
        pen.setWidth(2)
        pen.setStyle(Qt.PenStyle.DashLine)
        series.setPen(pen)
        series.append(QPointF(now_ms, 0.0))
        series.append(QPointF(now_ms, 90.0))
        return series

    # ------------------------------------------------------------------ #
    # Signal handlers
    # ------------------------------------------------------------------ #

    def _on_series_hovered(self, point: QPointF, state: bool) -> None:
        """Show satellite name + elevation as tooltip when hovering over a series."""
        sender = self.sender()
        if not isinstance(sender, QSplineSeries):
            return
        if state and sender in self._series_to_info:
            sat_name, max_el = self._series_to_info[sender]
            from PySide6.QtCore import QPoint  # noqa: PLC0415
            from PySide6.QtWidgets import QToolTip  # noqa: PLC0415

            chart_pt = self._chart.mapToPosition(point, sender)
            scene_pt = self._chart.mapToScene(chart_pt)
            view_pt = self._chart_view.mapFromScene(scene_pt)
            global_pt = self._chart_view.mapToGlobal(QPoint(int(view_pt.x()), int(view_pt.y())))
            QToolTip.showText(global_pt, f"{sat_name}\nMax El: {max_el:.1f}°")
