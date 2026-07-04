"""
QTH manual setup dialog.

Supports direct entry of latitude/longitude/elevation, Maidenhead grid locator
input, and callsign input.  Elevation can be fetched automatically via the
Open Elevation API (falls back to 0 m when offline).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import httpx
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.location import LocationManager, grid_to_latlon
from i18n import _

logger = logging.getLogger(__name__)

_OPENTOPODATA_URL = "https://api.opentopodata.org/v1/srtm90m"
_OPEN_ELEVATION_URL = "https://api.open-elevation.com/api/v1/lookup"
_ELEVATION_TIMEOUT = 10.0


class _ElevationBridge(QObject):
    """Signal bridge that passes elevation results from a worker thread to the main thread."""

    done: Signal = Signal(object)  # float | None


def _fetch_elevation_sync(lat: float, lon: float) -> float | None:
    """
    Fetch elevation synchronously (blocking).

    Priority:
        1. opentopodata.org (GET)
        2. open-elevation.com (POST)

    Returns:
        Elevation in metres, or None when both services fail.
    """
    # 1st: opentopodata
    try:
        resp = httpx.get(
            _OPENTOPODATA_URL,
            params={"locations": f"{lat},{lon}"},
            timeout=_ELEVATION_TIMEOUT,
        )
        print(f"[Elevation] opentopodata status={resp.status_code} body={resp.text[:200]}")
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results and results[0].get("elevation") is not None:
            return float(results[0]["elevation"])
    except Exception as exc:
        logger.debug("opentopodata fetch failed: %s", exc)
        print(f"[Elevation] opentopodata failed: {exc}")

    # 2nd: open-elevation (POST with JSON body)
    try:
        resp = httpx.post(
            _OPEN_ELEVATION_URL,
            json={"locations": [{"latitude": lat, "longitude": lon}]},
            timeout=_ELEVATION_TIMEOUT,
        )
        print(f"[Elevation] open-elevation status={resp.status_code} body={resp.text[:200]}")
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            return float(results[0]["elevation"])
    except Exception as exc:
        logger.debug("open-elevation fetch failed: %s", exc)
        print(f"[Elevation] open-elevation failed: {exc}")

    return None


class QTHDialog(QDialog):
    """QTH (observer location) setup dialog."""

    def __init__(
        self,
        location_manager: LocationManager,
        parent: QWidget | None = None,
    ) -> None:
        """
        Args:
            location_manager: Location manager instance
            parent:           Parent widget
        """
        super().__init__(parent)
        self._location_manager = location_manager
        self.setWindowTitle(_("Set QTH"))
        self.setMinimumWidth(440)

        # Debounce timer for the Grid Locator tab (auto-fetches elevation after 1 second)
        self._grid_debounce = QTimer(self)
        self._grid_debounce.setSingleShot(True)
        self._grid_debounce.setInterval(1000)
        self._grid_debounce.timeout.connect(self._auto_fetch_grid_elevation)

        self._build_ui()
        self._load_current()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self._tabs = QTabWidget()

        # Tab 0: Direct coordinate entry
        coord_tab = QWidget()
        coord_form = QFormLayout(coord_tab)

        self._lat_spin = QDoubleSpinBox()
        self._lat_spin.setRange(-90.0, 90.0)
        self._lat_spin.setDecimals(6)
        self._lat_spin.setSuffix("°")
        coord_form.addRow(_("Latitude (°N):"), self._lat_spin)

        self._lon_spin = QDoubleSpinBox()
        self._lon_spin.setRange(-180.0, 180.0)
        self._lon_spin.setDecimals(6)
        self._lon_spin.setSuffix("°")
        coord_form.addRow(_("Longitude (°E):"), self._lon_spin)

        # Elevation + Get Elevation button
        elev_row = QWidget()
        elev_h = QHBoxLayout(elev_row)
        elev_h.setContentsMargins(0, 0, 0, 0)
        self._elev_spin = QDoubleSpinBox()
        self._elev_spin.setRange(-100.0, 9000.0)
        self._elev_spin.setDecimals(1)
        self._elev_spin.setSuffix(" m")
        elev_h.addWidget(self._elev_spin)
        self._get_elev_btn = QPushButton(_("Get Elevation"))
        self._get_elev_btn.setFixedWidth(120)
        self._get_elev_btn.clicked.connect(self._on_get_coord_elevation)
        elev_h.addWidget(self._get_elev_btn)
        coord_form.addRow(_("Elevation (m):"), elev_row)

        self._tabs.addTab(coord_tab, _("Coordinates"))

        # Tab 1: Grid locator entry
        grid_tab = QWidget()
        grid_form = QFormLayout(grid_tab)

        self._grid_edit = QLineEdit()
        self._grid_edit.setPlaceholderText("")
        self._grid_edit.setMaxLength(6)
        grid_form.addRow(_("Grid Locator:"), self._grid_edit)

        self._grid_elev_spin = QDoubleSpinBox()
        self._grid_elev_spin.setRange(-100.0, 9000.0)
        self._grid_elev_spin.setDecimals(1)
        self._grid_elev_spin.setSuffix(" m")
        grid_form.addRow(_("Elevation (m):"), self._grid_elev_spin)

        self._grid_preview = QLabel("")
        grid_form.addRow(_("Decoded:"), self._grid_preview)

        self._grid_elev_status = QLabel("")
        grid_form.addRow("", self._grid_elev_status)

        self._grid_edit.textChanged.connect(self._on_grid_changed)

        self._tabs.addTab(grid_tab, _("Grid Locator"))

        layout.addWidget(self._tabs)

        # Callsign
        call_group = QGroupBox(_("Station"))
        call_form = QFormLayout(call_group)
        self._call_edit = QLineEdit()
        self._call_edit.setPlaceholderText("")
        self._call_edit.setMaxLength(20)
        call_form.addRow(_("Callsign:"), self._call_edit)
        layout.addWidget(call_group)

        # OK / Cancel buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_current(self) -> None:
        """Load the current settings into the input fields."""
        loc = self._location_manager.current
        if loc:
            self._lat_spin.setValue(loc.latitude_deg)
            self._lon_spin.setValue(loc.longitude_deg)
            self._elev_spin.setValue(loc.elevation_m)
            self._grid_elev_spin.setValue(loc.elevation_m)

        callsign = self._location_manager.get_callsign()
        if callsign:
            self._call_edit.setText(callsign)

    # ------------------------------------------------------------------ #
    # Automatic elevation fetch
    # ------------------------------------------------------------------ #

    def _fetch_elevation_async(
        self,
        lat: float,
        lon: float,
        on_done: Callable[[float | None], None],
    ) -> None:
        """Fetch elevation in a background thread and invoke the callback on the main thread.

        QTimer.singleShot called from threading.Thread does not fire because
        non-Qt threads have no event loop.  A Signal is used to queue the
        result back to the main thread so the UI can be updated safely.
        """
        bridge = _ElevationBridge(self)
        bridge.done.connect(on_done)

        def worker() -> None:
            elev = _fetch_elevation_sync(lat, lon)
            bridge.done.emit(elev)

        threading.Thread(target=worker, daemon=True).start()

    def _on_get_coord_elevation(self) -> None:
        """Handle the "Get Elevation" button press on the Coordinates tab."""
        lat = self._lat_spin.value()
        lon = self._lon_spin.value()
        self._get_elev_btn.setText(_("Getting..."))
        self._get_elev_btn.setEnabled(False)

        def on_done(elev: float | None) -> None:
            if elev is not None:
                self._elev_spin.setValue(elev)
                self._get_elev_btn.setText(_("Get Elevation"))
            else:
                self._get_elev_btn.setText(_("Unavailable"))
                QTimer.singleShot(2000, lambda: self._get_elev_btn.setText(_("Get Elevation")))
            self._get_elev_btn.setEnabled(True)

        self._fetch_elevation_async(lat, lon, on_done)

    def _auto_fetch_grid_elevation(self) -> None:
        """Grid Locator tab: auto-fetch elevation after the debounce delay."""
        g = self._grid_edit.text().strip()
        if len(g) not in (4, 6):
            return
        try:
            lat, lon = grid_to_latlon(g)
        except ValueError:
            return

        self._grid_elev_status.setText(_("Getting elevation..."))

        def on_done(elev: float | None) -> None:
            if elev is not None:
                self._grid_elev_spin.setValue(elev)
                self._grid_elev_status.setText(f"↑ {elev:.1f} m (auto)")
            else:
                self._grid_elev_status.setText(_("Elevation unavailable (offline?)"))

        self._fetch_elevation_async(lat, lon, on_done)

    # ------------------------------------------------------------------ #
    # Signal handlers
    # ------------------------------------------------------------------ #

    def _on_grid_changed(self, text: str) -> None:
        """Update the decode preview and schedule an elevation fetch on grid locator input."""
        g = text.strip()
        if len(g) in (4, 6):
            try:
                lat, lon = grid_to_latlon(g)
                ns = "N" if lat >= 0 else "S"
                ew = "E" if lon >= 0 else "W"
                self._grid_preview.setText(f"{abs(lat):.4f}°{ns}  {abs(lon):.4f}°{ew}")
                self._grid_elev_status.setText("")
                self._lat_spin.setValue(lat)
                self._lon_spin.setValue(lon)
                self._grid_debounce.start()
            except ValueError:
                self._grid_preview.setText(_("Invalid grid locator"))
                self._grid_debounce.stop()
                self._grid_elev_status.setText("")
        else:
            self._grid_preview.setText("")
            self._grid_debounce.stop()
            self._grid_elev_status.setText("")

    def _on_accept(self) -> None:
        """Handle the OK button press."""
        tab = self._tabs.currentIndex()
        try:
            if tab == 0:
                self._location_manager.from_manual(
                    self._lat_spin.value(),
                    self._lon_spin.value(),
                    self._elev_spin.value(),
                )
            else:
                grid = self._grid_edit.text().strip()
                if not grid:
                    QMessageBox.warning(self, _("Error"), _("Please enter a grid locator."))
                    return
                self._location_manager.from_grid(grid, self._grid_elev_spin.value())
                self._location_manager.save_grid(grid)

            callsign = self._call_edit.text().strip().upper()
            self._location_manager.save_callsign(callsign)
            self.accept()
        except ValueError as exc:
            QMessageBox.warning(self, _("Error"), str(exc))
