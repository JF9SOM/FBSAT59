"""
Unified ADIF log export dialog.

Queries ft4_log, q65_log, and aprs_log for a user-selected date range,
merges the records in chronological order, and writes a single .adi file.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from i18n import _
from ui.adif_utils import adif_write_or_append, build_adif_record


def _latlon_to_grid(lat: float, lon: float) -> str:
    """Convert latitude/longitude to a 4-character Maidenhead locator."""
    lon_adj = lon + 180.0
    lat_adj = lat + 90.0
    field_lon = int(lon_adj / 20)
    field_lat = int(lat_adj / 10)
    sq_lon = int((lon_adj % 20) / 2)
    sq_lat = int(lat_adj % 10)
    return chr(ord("A") + field_lon) + chr(ord("A") + field_lat) + str(sq_lon) + str(sq_lat)


class LogExportDialog(QDialog):
    """Date-range ADIF export dialog shared by APRS, FT4, and Q65 tabs.

    Args:
        conn:        SQLite connection that holds ft4_log, q65_log, aprs_log.
        my_call:     Operator callsign (used for APRS MY_CALL field).
        my_ssid:     Operator SSID (0 = no SSID).
        parent:      Parent widget.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        my_call: str = "",
        my_ssid: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._conn = conn
        self._my_call = my_call
        self._my_ssid = my_ssid

        self.setWindowTitle(_("Export Log (ADIF)"))
        self.setMinimumWidth(400)

        self._build_ui()
        self._refresh_count()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Date range
        range_box = QGroupBox(_("Date Range (UTC)"))
        form = QFormLayout(range_box)

        today = QDate.currentDate()
        first_of_month = today.addDays(-(today.day() - 1))

        self._from_edit = QDateEdit()
        self._from_edit.setCalendarPopup(True)
        self._from_edit.setDate(first_of_month)
        self._from_edit.setDisplayFormat("yyyy-MM-dd")
        form.addRow(_("From:"), self._from_edit)

        self._to_edit = QDateEdit()
        self._to_edit.setCalendarPopup(True)
        self._to_edit.setDate(today)
        self._to_edit.setDisplayFormat("yyyy-MM-dd")
        form.addRow(_("To:"), self._to_edit)

        layout.addWidget(range_box)

        # Mode checkboxes
        mode_box = QGroupBox(_("Include modes"))
        mode_h = QHBoxLayout(mode_box)
        self._chk_ft4 = QCheckBox("FT4")
        self._chk_ft4.setChecked(True)
        self._chk_q65 = QCheckBox("Q65")
        self._chk_q65.setChecked(True)
        self._chk_aprs = QCheckBox("APRS")
        self._chk_aprs.setChecked(True)
        mode_h.addWidget(self._chk_ft4)
        mode_h.addWidget(self._chk_q65)
        mode_h.addWidget(self._chk_aprs)
        mode_h.addStretch()
        layout.addWidget(mode_box)

        # Match count
        self._count_label = QLabel(_("Matching QSOs: —"))
        self._count_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self._count_label)

        # Save-as row
        save_box = QGroupBox(_("Output file"))
        save_h = QHBoxLayout(save_box)
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText(_("Click Browse… to choose location"))
        self._path_edit.setReadOnly(True)
        save_h.addWidget(self._path_edit)
        browse_btn = QPushButton(_("Browse…"))
        browse_btn.setFixedWidth(90)
        browse_btn.clicked.connect(self._on_browse)
        save_h.addWidget(browse_btn)
        layout.addWidget(save_box)

        # OK / Cancel
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._export_btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._export_btn.setText(_("Export"))
        self._export_btn.setEnabled(False)
        self._buttons.accepted.connect(self._on_export)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        # Wire up live count refresh
        self._from_edit.dateChanged.connect(self._schedule_refresh)
        self._to_edit.dateChanged.connect(self._schedule_refresh)
        self._chk_ft4.stateChanged.connect(self._schedule_refresh)
        self._chk_q65.stateChanged.connect(self._schedule_refresh)
        self._chk_aprs.stateChanged.connect(self._schedule_refresh)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self._refresh_count)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _schedule_refresh(self) -> None:
        self._debounce.start()

    def _date_range(self) -> tuple[str, str]:
        """Return (from_str, to_str) as 'YYYY-MM-DD' inclusive."""
        return (
            self._from_edit.date().toString("yyyy-MM-dd"),
            self._to_edit.date().toString("yyyy-MM-dd"),
        )

    def _default_filename(self) -> str:
        f, t = self._date_range()
        if f == t:
            return f"log_{f.replace('-', '')}.adi"
        return f"log_{f.replace('-', '')}-{t.replace('-', '')}.adi"

    def _refresh_count(self) -> None:
        n = len(self._collect_records())
        self._count_label.setText(_("Matching QSOs: {n}").format(n=n))
        # Keep Export enabled only when path is chosen and there are records
        self._export_btn.setEnabled(bool(self._path_edit.text()) and n > 0)

    def _collect_records(self) -> list[tuple[str, str]]:
        """Return list of (iso_datetime, adif_record_str) sorted by time."""
        from_d, to_d = self._date_range()
        # to_d inclusive → compare against YYYY-MM-DD 23:59:59
        to_dt = to_d + " 23:59:59"

        records: list[tuple[str, str]] = []

        # FT4
        if self._chk_ft4.isChecked():
            try:
                rows = self._conn.execute(
                    "SELECT qso_date, time_on, time_off, call, gridsquare, "
                    "rst_sent, rst_rcvd, freq_hz, sat_name FROM ft4_log "
                    "WHERE qso_date >= ? AND qso_date <= ? ORDER BY id ASC",
                    (from_d.replace("-", ""), to_d.replace("-", "")),
                ).fetchall()
                for r in rows:
                    qso_date, time_on, time_off, call, grid, rst_s, rst_r, freq_hz, sat = r
                    iso = f"{qso_date[:4]}-{qso_date[4:6]}-{qso_date[6:]} {time_on}"
                    freq_mhz = f"{freq_hz / 1e6:.6f}" if freq_hz else ""
                    record = build_adif_record(
                        {
                            "CALL": call or "",
                            "QSO_DATE": qso_date or "",
                            "TIME_ON": time_on or "",
                            "MODE": "FT4",
                            "PROP_MODE": "SAT",
                            "FREQ": freq_mhz,
                            "SAT_NAME": sat or "",
                            "RST_SENT": rst_s or "",
                            "RST_RCVD": rst_r or "",
                            "GRIDSQUARE": grid or "",
                        }
                    )
                    records.append((iso, record))
            except sqlite3.OperationalError:
                pass

        # Q65
        if self._chk_q65.isChecked():
            try:
                rows = self._conn.execute(
                    "SELECT qso_date, time_on, time_off, call, gridsquare, "
                    "rst_sent, rst_rcvd, freq_hz, sat_name FROM q65_log "
                    "WHERE qso_date >= ? AND qso_date <= ? ORDER BY qso_date, time_on",
                    (from_d.replace("-", ""), to_d.replace("-", "")),
                ).fetchall()
                for row in rows:
                    qso_date, time_on, time_off, call, grid, rst_s, rst_r, freq_hz, sat = row
                    iso = f"{qso_date[:4]}-{qso_date[4:6]}-{qso_date[6:]} {time_on}"
                    freq_mhz = f"{freq_hz / 1e6:.6f}" if freq_hz else ""
                    record = build_adif_record(
                        {
                            "CALL": call or "",
                            "QSO_DATE": qso_date or "",
                            "TIME_ON": time_on or "",
                            "TIME_OFF": time_off or time_on or "",
                            "MODE": "Q65",
                            "PROP_MODE": "SAT",
                            "FREQ": freq_mhz,
                            "SAT_NAME": sat or "",
                            "RST_SENT": rst_s or "",
                            "RST_RCVD": rst_r or "",
                            "GRIDSQUARE": grid or "",
                        }
                    )
                    records.append((iso, record))
            except sqlite3.OperationalError:
                pass

        # APRS
        if self._chk_aprs.isChecked():
            try:
                my_station = f"{self._my_call}-{self._my_ssid}" if self._my_ssid else self._my_call
                rows = self._conn.execute(
                    "SELECT received_at, callsign, via, latitude_deg, longitude_deg, "
                    "comment, norad_sat FROM aprs_log "
                    "WHERE received_at >= ? AND received_at <= ? ORDER BY id ASC",
                    (from_d, to_dt),
                ).fetchall()
                for row in rows:
                    ts_raw, callsign, via, lat_deg, lon_deg, comment, norad_sat = row
                    try:
                        dt = datetime.fromisoformat(str(ts_raw or "").replace(" ", "T"))
                    except ValueError:
                        dt = datetime.now(tz=UTC)
                    iso = dt.strftime("%Y-%m-%d %H:%M:%S")
                    qso_date = dt.strftime("%Y%m%d")
                    time_on = dt.strftime("%H%M%S")
                    cs = str(callsign or "").split(">")[0].split("-")[0]
                    sat_name = ""
                    if norad_sat:
                        try:
                            sat_row = self._conn.execute(
                                "SELECT name FROM satellites WHERE norad_cat_id = ?",
                                (norad_sat,),
                            ).fetchone()
                            if sat_row:
                                sat_name = str(sat_row[0])
                        except sqlite3.OperationalError:
                            pass
                    grid = ""
                    if lat_deg is not None:
                        grid = _latlon_to_grid(float(lat_deg), float(lon_deg or 0))
                    # APRS has no signal-report exchange, so a confirmed message
                    # exchange is logged as a nominal 599/599 report (see
                    # CLAUDE.md "ログソフト連携" design note).
                    fields = {
                        "CALL": cs,
                        "QSO_DATE": qso_date,
                        "TIME_ON": time_on,
                        "MODE": "APRS",
                        "MY_CALL": my_station,
                        "COMMENT": str(comment or ""),
                        "VIA": str(via or ""),
                        "SAT_NAME": sat_name,
                    }
                    if sat_name:
                        fields["PROP_MODE"] = "SAT"
                    fields["RST_SENT"] = "599"
                    fields["RST_RCVD"] = "599"
                    if grid:
                        fields["GRIDSQUARE"] = grid
                    records.append((iso, build_adif_record(fields)))
            except sqlite3.OperationalError:
                pass

        records.sort(key=lambda x: x[0])
        return records

    # ------------------------------------------------------------------ #
    # Slots
    # ------------------------------------------------------------------ #

    def _on_browse(self) -> None:
        path, _filt = QFileDialog.getSaveFileName(
            self,
            _("Save ADIF log"),
            self._default_filename(),
            "ADIF (*.adi);;All files (*)",
        )
        if path:
            self._path_edit.setText(path)
            self._refresh_count()

    def _on_export(self) -> None:
        path = self._path_edit.text().strip()
        if not path:
            return
        records = self._collect_records()
        if not records:
            return
        adif_write_or_append(path, "".join(r for _, r in records))
        self.accept()
        QMessageBox.information(
            self,
            _("Export"),
            _("Exported {n} QSOs to {f}").format(n=len(records), f=path),
        )
