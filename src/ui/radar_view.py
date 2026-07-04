"""
Radar chart (sky view) widget.

RadarView    — Polar radar display using PySide6 QPainter
SatTrackData — Container for satellite position and pass track data
az_el_to_xy  — Utility to convert azimuth/elevation to radar (x, y) coordinates

Radar coordinate system:
    Center   = zenith (elevation 90°)
    Outer    = horizon (elevation 0°)
    Top      = North (azimuth 0°), increasing clockwise (East = 90°)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from core.celestial_engine import MOON_ID
from i18n import _

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAT_COLORS: list[QColor] = [
    QColor("#e74c3c"),
    QColor("#3498db"),
    QColor("#2ecc71"),
    QColor("#f39c12"),
    QColor("#9b59b6"),
    QColor("#1abc9c"),
    QColor("#e67e22"),
    QColor("#34495e"),
]

_ELEVATION_RINGS: tuple[int, ...] = (0, 30, 60)
_CARDINALS: tuple[tuple[str, int], ...] = (("N", 0), ("E", 90), ("S", 180), ("W", 270))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SatTrackData:
    """
    Satellite position and pass track data for radar display.

    Attributes:
        name:            Satellite name (for label display)
        norad_cat_id:    NORAD catalog number
        azimuth_deg:     Current azimuth (degrees, North=0, East=90)
        elevation_deg:   Current elevation (degrees, 0=horizon, 90=zenith)
        is_visible:      Whether the satellite is above the horizon
        track:           Pass track [(az_deg, el_deg), ...] in AOS→LOS order
        aos_time:        AOS time (UTC) — next pass AOS when not visible, current AOS when visible
        los_time:        LOS time (UTC) — current pass LOS when visible, next pass LOS otherwise
        next_max_el:     Maximum elevation of the next (or current) pass (degrees)
        next_duration_s: Duration of the next (or current) pass (seconds)
    """

    name: str
    norad_cat_id: int
    azimuth_deg: float = 0.0
    elevation_deg: float = 0.0
    is_visible: bool = False
    track: list[tuple[float, float]] = field(default_factory=list)
    aos_time: datetime | None = None
    los_time: datetime | None = None
    next_max_el: float | None = None
    next_duration_s: float | None = None


# ---------------------------------------------------------------------------
# Utilities (UI-independent)
# ---------------------------------------------------------------------------


def az_el_to_xy(
    azimuth_deg: float,
    elevation_deg: float,
    cx: float,
    cy: float,
    radius: float,
) -> tuple[float, float]:
    """
    Convert azimuth/elevation to (x, y) on the polar radar.

    Args:
        azimuth_deg:   Azimuth in degrees (North=0, East=90)
        elevation_deg: Elevation in degrees (0=horizon, 90=zenith)
        cx, cy:        Radar center coordinates (pixels)
        radius:        Radius of the horizon circle (pixels)

    Returns:
        (x, y) pixel coordinates on the radar
    """
    el = max(0.0, min(90.0, elevation_deg))
    r = (90.0 - el) / 90.0 * radius
    az_rad = math.radians(azimuth_deg)
    x = cx + r * math.sin(az_rad)
    y = cy - r * math.cos(az_rad)
    return x, y


# ---------------------------------------------------------------------------
# RadarView widget
# ---------------------------------------------------------------------------


class RadarView(QWidget):
    """
    PySide6 widget that displays satellite positions and pass tracks on a polar radar.

    Usage::

        radar = RadarView()
        radar.set_tracks([
            SatTrackData(
                name="ISS", norad_cat_id=25544,
                azimuth_deg=45.0, elevation_deg=34.2, is_visible=True,
                track=[(0, 0), (45, 34), (90, 20)],
            ),
        ])
        layout.addWidget(radar)

    Signals:
        sat_clicked(str): emitted with the satellite name when a satellite dot is clicked
    """

    sat_clicked: Signal = Signal(str)

    def __init__(self, parent: QWidget | None = None, compact: bool = False) -> None:
        """
        Args:
            compact: use a smaller layout suited for the Comms Quick Panel's
                mini radar — smaller minimum size, no legend/status text
                (redundant with the Satellite Detail fields above it), and
                smaller cardinal labels so the circle can fill the widget.
        """
        super().__init__(parent)
        self._compact = compact
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        if compact:
            self.setMinimumSize(120, 120)
        else:
            self.setMinimumSize(200, 200)
        self._tracks: list[SatTrackData] = []
        self._dot_hit_radius: float = 10.0
        self._rot_az: float | None = None
        self._rot_el: float | None = None
        self._use_utc: bool = True

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def set_tracks(self, tracks: list[SatTrackData]) -> None:
        """
        Set the satellite list to display and repaint the radar.

        Args:
            tracks: List of SatTrackData (empty list clears the radar)
        """
        self._tracks = tracks
        self.update()

    def clear(self) -> None:
        """Clear all satellites from the radar."""
        self._tracks = []
        self.update()

    def set_use_utc(self, use_utc: bool) -> None:
        """Switch time display between UTC and local time."""
        self._use_utc = use_utc
        self.update()

    def _fmt_time(self, dt: datetime) -> str:
        """Format a UTC datetime as HH:MM UTC or HH:MM (local)."""
        if self._use_utc:
            return dt.astimezone(UTC).strftime("%H:%M") + " UTC"
        return dt.astimezone().strftime("%H:%M")

    def set_rotator_position(self, az: float | None, el: float | None) -> None:
        """Update the rotator current-position marker on the radar.

        Args:
            az: rotator azimuth in degrees, or None to hide the marker
            el: rotator elevation in degrees, or None to hide the marker
        """
        self._rot_az = az
        self._rot_el = el
        self.update()

    # ------------------------------------------------------------------ #
    # Qt event handlers
    # ------------------------------------------------------------------ #

    def sizeHint(self) -> QSize:
        return QSize(400, 400)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Emit sat_clicked when a click falls near a satellite dot."""
        cx, cy, r = self._radar_geometry()
        px = event.position().x()
        py = event.position().y()
        for track in reversed(self._tracks):
            sx, sy = az_el_to_xy(track.azimuth_deg, track.elevation_deg, cx, cy, r)
            if math.hypot(px - sx, py - sy) <= self._dot_hit_radius:
                self.sat_clicked.emit(track.name)
                return

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: ARG002
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            painter.fillRect(self.rect(), QColor("#0d1b2a"))
            self._draw(painter)
        finally:
            painter.end()

    # ------------------------------------------------------------------ #
    # Drawing helpers
    # ------------------------------------------------------------------ #

    def _radar_geometry(self) -> tuple[float, float, float]:
        """Return (center_x, center_y, radius) with bottom margin reserved for status text.

        Compact mode skips the status-text/legend reservation entirely (those
        are not drawn — see _draw()) and uses smaller label gutters, so the
        circle fills almost the whole widget instead of being squeezed small.
        """
        w = self.width()
        h = self.height()
        if self._compact:
            cx = w / 2.0
            cy = h / 2.0
            r = max(1.0, min(cx - 8.0, cy - 8.0) - 2.0)
            return cx, cy, max(r, 1.0)

        status_h = 70  # reserved for next-pass text below circle
        # Centre the circle in the full widget area.  r is bounded so that:
        #   - top:    circle + N label (≥20px) fits above cy
        #   - bottom: circle fits above the status-text area (status_h + 5px gutter)
        #   - sides:  W/E labels (≥15px gutter) fit inside widget width
        cx = w / 2.0
        cy = h / 2.0
        r = max(1.0, min(cx - 15.0, cy - 20.0, cy - status_h - 5.0) - 5.0)
        return cx, cy, max(r, 1.0)

    def _draw(self, p: QPainter) -> None:
        cx, cy, r = self._radar_geometry()
        self._draw_background(p, cx, cy, r)
        self._draw_rings(p, cx, cy, r)
        self._draw_crosshairs(p, cx, cy, r)
        self._draw_cardinals(p, cx, cy, r)

        for idx, track in enumerate(self._tracks):
            color = SAT_COLORS[idx % len(SAT_COLORS)]
            self._draw_track(p, track, color, cx, cy, r)
            self._draw_satellite(p, track, color, cx, cy, r)

        self._draw_rotator_marker(p, cx, cy, r)
        if not self._compact:
            self._draw_legend(p)
            self._draw_status(p, cx, cy, r)

    def _draw_background(self, p: QPainter, cx: float, cy: float, r: float) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#1a1a2e"))
        p.drawEllipse(int(cx - r), int(cy - r), int(r * 2), int(r * 2))
        p.setPen(QPen(QColor("#4a4a6a"), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(int(cx - r), int(cy - r), int(r * 2), int(r * 2))

    def _draw_rings(self, p: QPainter, cx: float, cy: float, r: float) -> None:
        label_font = QFont()
        label_font.setPointSize(6 if self._compact else 7)
        p.setFont(label_font)

        for el in _ELEVATION_RINGS:
            cr = int((90 - el) / 90.0 * r)
            pen = QPen(QColor("#2c3e50"), 1)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(int(cx) - cr, int(cy) - cr, cr * 2, cr * 2)
            # Skip the degree label in compact mode — the outer ring sits
            # right at the widget edge there, leaving no room for text.
            if not self._compact:
                p.setPen(QColor("#7f8c8d"))
                p.drawText(int(cx) + cr + 2, int(cy) + 5, f"{el}°")

    def _draw_crosshairs(self, p: QPainter, cx: float, cy: float, r: float) -> None:
        pen = QPen(QColor("#2c3e50"), 1)
        pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawLine(int(cx), int(cy - r), int(cx), int(cy + r))
        p.drawLine(int(cx - r), int(cy), int(cx + r), int(cy))

    def _draw_cardinals(self, p: QPainter, cx: float, cy: float, r: float) -> None:
        font = QFont()
        font.setPointSize(6 if self._compact else 9)
        font.setBold(True)
        p.setFont(font)

        for label, az in _CARDINALS:
            x, y = az_el_to_xy(float(az), 0.0, cx, cy, r)
            offset = 8 if self._compact else 14
            if az == 0:  # N — top
                x -= 4.0
                y -= float(offset - 4)
            elif az == 90:  # E — right
                x += 4.0
                y += 4.0
            elif az == 180:  # S — bottom
                x -= 4.0
                y += float(offset)
            else:  # W — left
                x -= float(offset + 2)
                y += 4.0
            color = QColor("#e74c3c") if label == "N" else QColor("#bdc3c7")
            p.setPen(color)
            p.drawText(int(x), int(y), label)

    def _draw_track(
        self,
        p: QPainter,
        track: SatTrackData,
        color: QColor,
        cx: float,
        cy: float,
        r: float,
    ) -> None:
        if len(track.track) < 2:
            return

        if track.norad_cat_id == MOON_ID:
            self._draw_moon_track(p, track, cx, cy, r)
            return

        _TRACK_COLOR = QColor("#00bcd4")  # cyan
        _AOS_COLOR = QColor("#4caf50")  # green
        _LOS_COLOR = QColor("#f44336")  # red

        pts = [az_el_to_xy(az, el, cx, cy, r) for az, el in track.track]

        # Cyan track line
        p.setPen(QPen(_TRACK_COLOR, 2))
        for i in range(len(pts) - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            p.drawLine(int(x0), int(y0), int(x1), int(y1))

        # AOS point (green filled circle)
        ax, ay = pts[0]
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_AOS_COLOR)
        p.drawEllipse(int(ax) - 4, int(ay) - 4, 8, 8)

        # LOS point (red filled circle)
        lx, ly = pts[-1]
        p.setBrush(_LOS_COLOR)
        p.drawEllipse(int(lx) - 4, int(ly) - 4, 8, 8)

        p.setBrush(Qt.BrushStyle.NoBrush)

        label_font = QFont()
        label_font.setPointSize(8)
        p.setFont(label_font)
        p.setPen(_TRACK_COLOR)

        if track.aos_time is not None:
            p.drawText(int(ax) + 6, int(ay) - 2, f"AOS {self._fmt_time(track.aos_time)}")

        if track.los_time is not None:
            p.drawText(int(lx) + 6, int(ly) + 10, f"LOS {self._fmt_time(track.los_time)}")

    def _draw_moon_track(
        self,
        p: QPainter,
        track: SatTrackData,
        cx: float,
        cy: float,
        r: float,
    ) -> None:
        """Draw the Moon's 24-hour arc as a silver dashed line.

        Large AZ jumps (>180°) indicate the Moon crossing the radar boundary
        (e.g. going below the horizon and returning); these segments are skipped.
        """
        _MOON_TRACK_COLOR = QColor("#c8d8f8")  # pale blue-silver
        pts = [az_el_to_xy(az, el, cx, cy, r) for az, el in track.track]
        pen = QPen(_MOON_TRACK_COLOR, 1.5, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(len(pts) - 1):
            az0 = track.track[i][0]
            az1 = track.track[i + 1][0]
            # Skip segments where the Moon jumps across the radar (>180° AZ change)
            if abs(az1 - az0) > 180.0:
                continue
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            p.drawLine(int(x0), int(y0), int(x1), int(y1))

    def _draw_satellite(
        self,
        p: QPainter,
        track: SatTrackData,
        color: QColor,
        cx: float,
        cy: float,
        r: float,
    ) -> None:
        x, y = az_el_to_xy(track.azimuth_deg, track.elevation_deg, cx, cy, r)

        if track.norad_cat_id == MOON_ID:
            self._draw_moon_icon(p, x, y)
            label_font = QFont()
            label_font.setPointSize(8)
            p.setFont(label_font)
            p.setPen(QColor("#c8d8f8"))
            p.drawText(int(x) + 12, int(y) + 4, "Moon")
            return

        dot_r = 6
        # Current position: red hollow circle (same style whether visible or not)
        p.setPen(QPen(QColor("#f44336"), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(int(x) - dot_r, int(y) - dot_r, dot_r * 2, dot_r * 2)

        label_font = QFont()
        label_font.setPointSize(8)
        p.setFont(label_font)
        p.setPen(color)
        p.drawText(int(x) + dot_r + 2, int(y) + 4, track.name)

    def _draw_moon_icon(self, p: QPainter, x: float, y: float) -> None:
        """Draw a crescent moon icon centred at (x, y)."""
        _MOON_COLOR = QColor("#c8d8f8")  # pale blue-silver
        size = 9
        # Outer filled circle
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_MOON_COLOR)
        p.drawEllipse(QPointF(x, y), size, size)
        # Inner offset circle in background colour to create crescent
        p.setBrush(QColor("#0d1b2a"))
        p.drawEllipse(QPointF(x + size * 0.45, y - size * 0.1), size * 0.82, size * 0.82)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_legend(self, p: QPainter) -> None:
        """Draw a small legend in the top-right corner."""
        font = QFont()
        font.setPointSize(8)
        p.setFont(font)

        x = self.width() - 155
        y = 18
        line_h = 16

        # Satellite position: blue filled circle
        p.setPen(QColor("#3498db"))
        p.setBrush(QColor("#3498db"))
        p.drawEllipse(x, y - 7, 8, 8)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawText(x + 12, y, "Satellite Position")

        # Rotator position: orange ×
        rot_y = y + line_h
        mx, my = x + 4, rot_y - 2  # center of the × icon (4px half-size)
        p.setPen(QPen(QColor("#FF8C00"), 2))
        p.drawLine(mx - 4, my - 4, mx + 4, my + 4)
        p.drawLine(mx - 4, my + 4, mx + 4, my - 4)
        p.setPen(QColor("#FF8C00"))
        p.drawText(x + 12, rot_y, "Rotator Position")

    def _draw_rotator_marker(self, p: QPainter, cx: float, cy: float, r: float) -> None:
        """Draw an × marker at the rotator's current AZ/EL position."""
        if self._rot_az is None or self._rot_el is None:
            return
        x, y = az_el_to_xy(self._rot_az, self._rot_el, cx, cy, r)
        sz = 6.0
        pen = QPen(QColor("#FF8C00"), 2)
        p.setPen(pen)
        p.drawLine(int(x - sz), int(y - sz), int(x + sz), int(y + sz))
        p.drawLine(int(x - sz), int(y + sz), int(x + sz), int(y - sz))

    def _draw_status(self, p: QPainter, cx: float, cy: float, r: float) -> None:
        """Draw current or next pass info below the radar circle."""
        font = QFont()
        font.setPointSize(12)
        p.setFont(font)

        y_base = int(cy + r + 16)
        line_h = 22

        for i, track in enumerate(self._tracks):
            if track.is_visible:
                # IN PASS: show current pass info in green
                los_str = ""
                if track.los_time is not None:
                    los_str = f"  LOS: {self._fmt_time(track.los_time)}"
                text = (
                    f"IN PASS  EL: {track.elevation_deg:.1f}°"
                    f"  AZ: {track.azimuth_deg:.1f}°{los_str}"
                )
                p.setPen(QColor("#2ecc71"))
            elif track.aos_time is not None:
                # Next pass info: "Next: MM/DD HH:MM [UTC]  Max X.X°  Xm Ys"
                aos_dt = track.aos_time if self._use_utc else track.aos_time.astimezone()
                aos_str = aos_dt.strftime("%m/%d %H:%M") + (" UTC" if self._use_utc else "")
                max_el_str = (
                    f"  Max {track.next_max_el:.1f}°" if track.next_max_el is not None else ""
                )
                dur_str = ""
                if track.next_duration_s is not None:
                    m = int(track.next_duration_s) // 60
                    s = int(track.next_duration_s) % 60
                    dur_str = f"  {m}m {s:02d}s"
                text = f"Next: {aos_str}{max_el_str}{dur_str}"
                p.setPen(QColor("#ffffff"))
            else:
                # No upcoming pass found — satellite may never rise from this QTH
                text = _("No visible passes from this location")
                p.setPen(QColor("#8b949e"))

            p.drawText(
                0,
                y_base + i * line_h,
                self.width(),
                line_h,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                text,
            )
