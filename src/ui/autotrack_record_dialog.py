"""
Autotrack / Record dialog.

Combines Autotrack list management, enable/disable control,
and SDR recording (Audio + IQ) settings in one place.
Opened from the "Autotrack/Record" menu in the main menu bar.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from PySide6.QtCore import QDateTime, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from i18n import _

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_RECORDING_SETTINGS_KEY = "autotrack_recording_settings"
_AUTOTRACK_ENABLED_KEY = "autotrack_enabled"


class _SatSearchDialog(QDialog):
    """Satellite picker dialog with a live text-search filter."""

    def __init__(
        self,
        satellites: list[tuple[int, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Add Satellite"))
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


_AUTOTRACK_HELP = (
    "Autotrack — Sequential Satellite Tracking\n"
    "\n"
    "Autotrack automatically connects the rig and rotator at AOS,\n"
    "switches Doppler correction between satellites in priority order,\n"
    "and disconnects at LOS.\n"
    "\n"
    "Satellite switching rules:\n"
    "\n"
    "1. Current satellite is above Min El → keep tracking.\n"
    "\n"
    "2. Current satellite drops below Min El:\n"
    "   a. Another satellite is already visible\n"
    "      → switch immediately (list order as tiebreak).\n"
    "   b. No satellite is visible yet\n"
    "      → switch to the one with the earliest AOS\n"
    "        (list order as tiebreak on equal AOS).\n"
    "\n"
    "3. Overlapping passes: never interrupt a pass in progress.\n"
    "   Wait for the current satellite's LOS before switching.\n"
    "\n"
    "Prerequisites:\n"
    "  1. Create an Autotrack list and add satellites below.\n"
    "  2. Run a pass search in Upcoming Passes > Group tab.\n"
    "  3. Select the list and check 'Enable Autotrack'.\n"
    "\n"
    "Recording (SDR):\n"
    "  When an SDR is connected, Audio and/or IQ recording\n"
    "  starts automatically at AOS and stops at LOS."
)


class AutotrackRecordDialog(QDialog):
    """
    Non-modal dialog for Autotrack list management and recording settings.

    Signals
    -------
    autotrack_toggled(bool)
        Emitted when the Enable Autotrack checkbox changes.
    autotrack_list_changed(object)
        Emitted when the selected list changes (int list_id or None).
    audio_record_changed(bool)
        Emitted when the Audio Record checkbox changes.
    iq_record_changed(bool)
        Emitted when the IQ Record checkbox changes.
    lists_modified()
        Emitted when lists/entries are added, removed, or reordered
        (callers should reload their list combos).
    """

    autotrack_toggled: Signal = Signal(bool)
    autotrack_list_changed: Signal = Signal(object)
    audio_record_changed: Signal = Signal(bool)
    iq_record_changed: Signal = Signal(bool)
    meteor_record_changed: Signal = Signal(bool)
    lists_modified: Signal = Signal()

    def __init__(self, conn: sqlite3.Connection, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Autotrack / Record"))
        self.setMinimumSize(760, 520)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
        self._conn = conn
        self._at_selected_list_id: int | None = None
        self._use_utc: bool = True
        self._setup_ui()
        self._reload_at_lists()
        self._load_recording_settings()
        self._load_autotrack_enabled_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def populate_list_combo(self, lists: list[dict[str, int | str]]) -> None:
        """Refresh the list selector combo from external data.

        Preserves the current selection across a refresh where possible
        (e.g. renaming some other list), and notifies autotrack_list_changed
        if the effective selection actually changed as a result of this
        call (e.g. the previously-selected list was just deleted, or this
        is the first list ever created and the combo now auto-selects it).

        This emit is necessary because the rebuild below is wrapped in
        blockSignals() -- needed to suppress the spurious intermediate
        currentIndexChanged events Qt fires during clear()/addItem() -- so
        without it, whatever list the combo ends up on after a refresh
        would never actually reach MainWindow (GitHub Issue #27 follow-up,
        2026-08-23): a user created a single list, added a satellite entry,
        and checked "Enable Autotrack" -- the combo clearly showed that
        list selected, but MainWindow's AutotrackManager had never been
        told, so entries() stayed empty and Autotrack silently did nothing
        ("Run a pass search first", even though a pass search wasn't
        actually the missing piece). It went unnoticed because a lone list
        looks selected the moment it's created -- there's nothing prompting
        the user to reselect it by hand, which is the only thing that fires
        currentIndexChanged normally.
        """
        previous_id = self._at_sel_combo.currentData()
        self._at_sel_combo.blockSignals(True)
        self._at_sel_combo.clear()
        for lst in lists:
            self._at_sel_combo.addItem(str(lst["name"]), userData=lst["id"])
        self._at_sel_combo.setEnabled(bool(lists))
        self._at_enable_cb.setEnabled(bool(lists))
        if not lists:
            self._at_enable_cb.setChecked(False)
        if previous_id is not None:
            idx = self._at_sel_combo.findData(previous_id)
            if idx >= 0:
                self._at_sel_combo.setCurrentIndex(idx)
        new_id = self._at_sel_combo.currentData()
        self._at_sel_combo.blockSignals(False)
        if new_id != previous_id:
            self.autotrack_list_changed.emit(new_id)

    def current_list_id(self) -> int | None:
        """Return the Autotrack List currently selected in the combo, if any."""
        list_id = self._at_sel_combo.currentData()
        return int(list_id) if isinstance(list_id, int) else None

    def set_autotrack_enabled(self, enabled: bool) -> None:
        """Programmatically set the Enable Autotrack checkbox."""
        self._at_enable_cb.blockSignals(True)
        self._at_enable_cb.setChecked(enabled)
        self._at_enable_cb.blockSignals(False)

    def is_autotrack_enabled(self) -> bool:
        """Return the Enable Autotrack checkbox's current (possibly
        just-restored) state."""
        return bool(self._at_enable_cb.isChecked())

    def set_autotrack_status(self, text: str, ok: bool = True) -> None:
        """Update the status label."""
        self._at_status_label.setText(text)
        color = "#2ecc71" if ok else "#e74c3c"
        self._at_status_label.setStyleSheet(f"color: {color};")

    def is_audio_record_enabled(self) -> bool:
        return bool(self._audio_rec_cb.isChecked())

    def is_meteor_record_enabled(self) -> bool:
        return bool(self._meteor_rec_cb.isChecked())

    def is_iq_record_enabled(self) -> bool:
        return bool(self._iq_rec_cb.isChecked())

    def set_use_utc(self, use_utc: bool) -> None:
        """Switch the timer start input between UTC and local time display."""
        from PySide6.QtCore import QTimeZone  # noqa: PLC0415

        if use_utc == self._use_utc:
            return
        old_ts = int(self.get_timer_start_utc().timestamp())
        self._use_utc = use_utc
        tz = QTimeZone.utc() if use_utc else QTimeZone.systemTimeZone()
        self._timer_start_label.setText(_("Start (UTC):") if use_utc else _("Start (Local):"))
        self._timer_start_dt.setDateTime(QDateTime.fromSecsSinceEpoch(old_ts, tz))

    def _on_now_clicked(self) -> None:
        from PySide6.QtCore import QTimeZone  # noqa: PLC0415

        if self._use_utc:
            self._timer_start_dt.setDateTime(
                QDateTime.currentDateTimeUtc().toTimeZone(QTimeZone.utc())
            )
        else:
            self._timer_start_dt.setDateTime(QDateTime.currentDateTime())

    def get_timer_start_utc(self) -> datetime:
        """Return the configured start time as UTC datetime."""
        ts = self._timer_start_dt.dateTime().toSecsSinceEpoch()
        return datetime.fromtimestamp(ts, tz=UTC)

    def get_timer_stop_utc(self) -> datetime:
        """Return the configured stop time (start + duration) as UTC datetime."""
        hours: int = self._timer_dur_combo.currentData()
        return self.get_timer_start_utc() + timedelta(hours=hours)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        # ── Autotrack Lists (top) ─────────────────────────────────────
        mgmt_group = QGroupBox(_("Autotrack Lists"))
        mgmt_layout = QHBoxLayout(mgmt_group)

        # Left: list of lists
        left = QVBoxLayout()
        left.addWidget(QLabel(_("Lists:")))
        self._at_list_widget = QListWidget()
        self._at_list_widget.setMaximumWidth(180)
        self._at_list_widget.currentRowChanged.connect(self._on_at_list_selected)
        left.addWidget(self._at_list_widget)

        list_btn_row = QHBoxLayout()
        add_list_btn = QPushButton(_("Add"))
        add_list_btn.clicked.connect(self._on_at_add_list)
        rename_list_btn = QPushButton(_("Rename"))
        rename_list_btn.clicked.connect(self._on_at_rename_list)
        del_list_btn = QPushButton(_("Delete"))
        del_list_btn.clicked.connect(self._on_at_delete_list)
        list_btn_row.addWidget(add_list_btn)
        list_btn_row.addWidget(rename_list_btn)
        list_btn_row.addWidget(del_list_btn)
        left.addLayout(list_btn_row)
        mgmt_layout.addLayout(left)

        # Right: entries
        right = QVBoxLayout()
        right.addWidget(QLabel(_("Entries (satellite + transmitter):")))
        self._at_entry_tree = QTreeWidget()
        self._at_entry_tree.setHeaderLabels(
            [_("Satellite"), _("Transmitter"), _("DL (MHz)"), _("UL (MHz)"), _("Mode")]
        )
        self._at_entry_tree.setColumnWidth(0, 140)
        self._at_entry_tree.setColumnWidth(1, 180)
        self._at_entry_tree.setColumnWidth(2, 80)
        self._at_entry_tree.setColumnWidth(3, 80)
        right.addWidget(self._at_entry_tree)

        entry_btn_row = QHBoxLayout()
        add_entry_btn = QPushButton(_("Add Satellite…"))
        add_entry_btn.clicked.connect(self._on_at_add_entry)
        remove_entry_btn = QPushButton(_("Remove"))
        remove_entry_btn.clicked.connect(self._on_at_remove_entry)
        up_btn = QPushButton(_("▲"))
        up_btn.setFixedWidth(30)
        up_btn.clicked.connect(self._on_at_move_up)
        down_btn = QPushButton(_("▼"))
        down_btn.setFixedWidth(30)
        down_btn.clicked.connect(self._on_at_move_down)
        entry_btn_row.addWidget(add_entry_btn)
        entry_btn_row.addWidget(remove_entry_btn)
        entry_btn_row.addStretch()
        entry_btn_row.addWidget(up_btn)
        entry_btn_row.addWidget(down_btn)
        right.addLayout(entry_btn_row)
        mgmt_layout.addLayout(right)

        outer.addWidget(mgmt_group)

        # ── Autotrack Control ─────────────────────────────────────────
        ctrl_group = QGroupBox(_("Autotrack Control"))
        ctrl_form = QFormLayout(ctrl_group)
        ctrl_form.setSpacing(6)

        self._at_sel_combo = QComboBox()
        self._at_sel_combo.setEnabled(False)
        self._at_sel_combo.currentIndexChanged.connect(self._on_sel_combo_changed)
        ctrl_form.addRow(_("List:"), self._at_sel_combo)

        enable_row = QHBoxLayout()
        self._at_enable_cb = QCheckBox(_("Enable Autotrack"))
        self._at_enable_cb.setEnabled(False)
        self._at_enable_cb.toggled.connect(self._on_enable_toggled)
        enable_row.addWidget(self._at_enable_cb)
        enable_row.addStretch()
        help_btn = QPushButton("?")
        help_btn.setFixedSize(22, 22)
        help_btn.setFlat(True)
        help_btn.setToolTip(_AUTOTRACK_HELP)
        help_btn.setStyleSheet(
            "QPushButton { border: 1px solid gray; border-radius: 11px; font-weight: bold; }"
        )
        enable_row.addWidget(help_btn)
        ctrl_form.addRow(enable_row)

        self._at_status_label = QLabel("—")
        self._at_status_label.setWordWrap(True)
        # Status text like "Next: METEOR M2-3 in 463 min" wraps to two
        # lines under some fonts (confirmed on macOS; the same text fits on
        # one line on Windows) -- QFormLayout doesn't always reserve enough
        # row height for a wrapped QLabel's second line on its own, so the
        # second line was rendered but clipped by the row below it.
        # Reserve two lines' worth of height explicitly instead.
        self._at_status_label.setMinimumHeight(self._at_status_label.fontMetrics().height() * 2 + 4)
        ctrl_form.addRow(_("Status:"), self._at_status_label)

        outer.addWidget(ctrl_group)

        # ── Recording ─────────────────────────────────────────────────
        rec_group = QGroupBox(_("Recording (SDR)  — starts at AOS, stops at LOS"))
        rec_layout = QHBoxLayout(rec_group)
        self._audio_rec_cb = QCheckBox(_("Audio Record (MP3)"))
        self._audio_rec_cb.toggled.connect(self._on_audio_rec_toggled)
        self._iq_rec_cb = QCheckBox(_("IQ Record"))
        self._iq_rec_cb.toggled.connect(self._on_iq_rec_toggled)
        self._meteor_rec_cb = QCheckBox(_("METEOR / HRPT Reception"))
        self._meteor_rec_cb.setToolTip(
            _(
                "When the tracked satellite supports LRPT or HRPT,\n"
                "automatically open the METEOR/HRPT tab and start\n"
                "SatDump reception at AOS.  Stops at LOS."
            )
        )
        self._meteor_rec_cb.toggled.connect(self._on_meteor_rec_toggled)
        rec_layout.addWidget(self._audio_rec_cb)
        rec_layout.addWidget(self._iq_rec_cb)
        rec_layout.addWidget(self._meteor_rec_cb)
        rec_layout.addStretch()
        outer.addWidget(rec_group)

        # ── Autotrack Timer ───────────────────────────────────────────
        timer_group = QGroupBox(_("Autotrack Timer"))
        timer_form = QFormLayout(timer_group)
        timer_form.setSpacing(6)

        # Start time: QDateTimeEdit with calendar popup + "Now" reset button
        from PySide6.QtCore import QTimeZone  # noqa: PLC0415

        start_row = QHBoxLayout()
        self._timer_start_dt = QDateTimeEdit()
        self._timer_start_dt.setDisplayFormat("yyyy-MM-dd HH:mm")
        self._timer_start_dt.setCalendarPopup(True)
        # Default: UTC mode — show current time in UTC
        self._timer_start_dt.setDateTime(QDateTime.currentDateTimeUtc().toTimeZone(QTimeZone.utc()))
        start_row.addWidget(self._timer_start_dt)
        now_btn = QPushButton(_("Now"))
        now_btn.setFixedWidth(48)
        now_btn.setToolTip(_("Reset start time to current time"))
        now_btn.clicked.connect(self._on_now_clicked)
        start_row.addWidget(now_btn)
        self._timer_start_label = QLabel(_("Start (UTC):"))
        timer_form.addRow(self._timer_start_label, start_row)

        # Stop: duration combo
        self._timer_dur_combo = QComboBox()
        for label, hours in [
            (_("3 hours"), 3),
            (_("6 hours"), 6),
            (_("12 hours"), 12),
            (_("24 hours"), 24),
        ]:
            self._timer_dur_combo.addItem(label, userData=hours)
        timer_form.addRow(_("Stop after:"), self._timer_dur_combo)

        outer.addWidget(timer_group)

        # Close button
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btn_box.rejected.connect(self.hide)
        outer.addWidget(btn_box)

    # ------------------------------------------------------------------
    # Slots — Autotrack control
    # ------------------------------------------------------------------

    def _on_sel_combo_changed(self, _idx: int) -> None:
        list_id = self._at_sel_combo.currentData()
        has_list = list_id is not None
        self._at_enable_cb.setEnabled(has_list)
        if not has_list:
            self._at_enable_cb.setChecked(False)
        self.autotrack_list_changed.emit(list_id)

    def _on_enable_toggled(self, checked: bool) -> None:
        self.autotrack_toggled.emit(checked)
        self._save_autotrack_enabled_state(checked)
        self._warn_restart_required()

    def _warn_restart_required(self) -> None:
        """Tell the user to restart the app after toggling Enable Autotrack.

        A restart-after-toggle workflow was chosen (2026-08-23, GitHub
        Issue #27) as the reliable way to run Autotrack's SDR-driven
        METEOR/HRPT reception: a fresh process guarantees no rig/SDR handle
        from a previous session or a previous toggle is still open when
        Autotrack starts using the device, which intermittent
        "device already claimed"/"no device" failures at AOS traced back
        to. This is deliberately a blunt, always-shown warning rather than
        detecting the specific unsafe states -- it's cheap for the user to
        follow and doesn't depend on correctly enumerating every way a
        stale handle could linger.
        """
        QMessageBox.warning(
            self,
            _("Restart Required"),
            _(
                "Please restart FBSAT59 now.\n\n"
                "Changing Enable Autotrack can leave the rig/SDR connection "
                "in a state that prevents Autotrack from reliably starting "
                "or stopping METEOR/HRPT reception at AOS/LOS. Restarting "
                "the app guarantees a clean state."
            ),
        )

    def _load_autotrack_enabled_state(self) -> None:
        """Restore the Enable Autotrack checkbox from app_settings.

        Without this, "Enable Autotrack" always reset to unchecked on
        every app restart -- but worse, the Autotrack Timer's own
        auto-start logic (whose "start time" field always defaults to
        "now" on every restart) would then immediately re-enable it
        anyway on the very next tick, so a user who deliberately
        unchecked it and closed the dialog would find it checked again
        next time they opened the app (GitHub Issue #27 follow-up,
        2026-08-23). See MainWindow._check_autotrack()'s
        _autotrack_timer_armed guard for the other half of that fix.
        Signals are blocked here for the same reason as
        _load_recording_settings() -- MainWindow reads the restored value
        via is_autotrack_enabled() itself, after wiring up its own signals.
        """
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (_AUTOTRACK_ENABLED_KEY,),
        ).fetchone()
        enabled = bool(row and row["value"] == "1")
        self._at_enable_cb.blockSignals(True)
        self._at_enable_cb.setChecked(enabled)
        self._at_enable_cb.blockSignals(False)

    def _save_autotrack_enabled_state(self, enabled: bool) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at)"
            " VALUES (?, ?, CURRENT_TIMESTAMP)",
            (_AUTOTRACK_ENABLED_KEY, "1" if enabled else "0"),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Slots — Recording checkboxes (persisted to app_settings)
    # ------------------------------------------------------------------

    def _on_audio_rec_toggled(self, checked: bool) -> None:
        self.audio_record_changed.emit(checked)
        self._save_recording_settings()

    def _on_iq_rec_toggled(self, checked: bool) -> None:
        self.iq_record_changed.emit(checked)
        self._save_recording_settings()

    def _on_meteor_rec_toggled(self, checked: bool) -> None:
        self.meteor_record_changed.emit(checked)
        self._save_recording_settings()

    def _load_recording_settings(self) -> None:
        """Restore the Audio/IQ/METEOR checkboxes from app_settings.

        These checkboxes used to reset to unchecked on every app restart
        (GitHub Issue #27 follow-up, 2026-08-22): a user had "METEOR / HRPT
        Reception" enabled in one session, but after restarting the app for
        a later pass the checkbox was silently back to off, so Autotrack's
        AOS handler never opened the METEOR tab even though "Tracking: ..."
        showed the satellite was being tracked correctly. Signals are
        blocked here since this only restores UI state -- MainWindow syncs
        its own copy of these flags from the getters below right after
        connecting the *_changed signals (see MainWindow.__init__()), not
        from a signal fired during this restore.
        """
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (_RECORDING_SETTINGS_KEY,),
        ).fetchone()
        if not row or not row["value"]:
            return
        try:
            settings = json.loads(str(row["value"]))
        except (TypeError, ValueError):
            return
        for checkbox, key in (
            (self._audio_rec_cb, "audio_record"),
            (self._iq_rec_cb, "iq_record"),
            (self._meteor_rec_cb, "meteor_record"),
        ):
            checkbox.blockSignals(True)
            checkbox.setChecked(bool(settings.get(key, False)))
            checkbox.blockSignals(False)

    def _save_recording_settings(self) -> None:
        settings = {
            "audio_record": self._audio_rec_cb.isChecked(),
            "iq_record": self._iq_rec_cb.isChecked(),
            "meteor_record": self._meteor_rec_cb.isChecked(),
        }
        self._conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at)"
            " VALUES (?, ?, CURRENT_TIMESTAMP)",
            (_RECORDING_SETTINGS_KEY, json.dumps(settings)),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Slots — List management
    # ------------------------------------------------------------------

    def _reload_at_lists(self) -> None:
        from core.autotrack import AutotrackManager

        lists = AutotrackManager.get_all_lists(self._conn)
        self._at_list_widget.clear()
        for lst in lists:
            item = QListWidgetItem(str(lst["name"]))
            item.setData(0x0100, int(lst["id"]))
            self._at_list_widget.addItem(item)
        self.populate_list_combo(lists)
        self.lists_modified.emit()

    def _reload_at_entries(self) -> None:
        from core.autotrack import AutotrackManager

        self._at_entry_tree.clear()
        if self._at_selected_list_id is None:
            return

        def _fmt_mhz(hz: int | str | None) -> str:
            return f"{int(hz) / 1_000_000:.3f}" if isinstance(hz, int) else "—"

        for entry in AutotrackManager.get_entries(self._conn, self._at_selected_list_id):
            item = QTreeWidgetItem(
                [
                    str(entry.get("sat_name") or entry["norad_cat_id"]),
                    str(entry.get("xpdr_desc") or entry["xpdr_uuid"]),
                    _fmt_mhz(entry.get("downlink_low")),
                    _fmt_mhz(entry.get("uplink_low")),
                    str(entry.get("mode") or ""),
                ]
            )
            item.setData(0, 0x0100, entry["id"])
            self._at_entry_tree.addTopLevelItem(item)

        # Adding/removing/reordering entries changes what Autotrack has to
        # track, so re-announce via the same signal used for list add/remove
        # (main_window.py re-tries the pass-prediction warm-up on this —
        # necessary when a list started out empty, warmed up as a no-op, and
        # only later got its first satellite added; without this the Autotrack
        # Timer's auto-start path never becomes ready, GitHub Issue #27).
        self.lists_modified.emit()

    def _on_at_list_selected(self, row: int) -> None:
        item = self._at_list_widget.item(row)
        self._at_selected_list_id = int(item.data(0x0100)) if item else None
        self._reload_at_entries()

    def _on_at_add_list(self) -> None:
        from core.autotrack import AutotrackManager

        name, ok = QInputDialog.getText(self, _("New Autotrack List"), _("List name:"))
        if ok and name.strip():
            AutotrackManager.create_list(self._conn, name.strip())
            self._reload_at_lists()

    def _on_at_rename_list(self) -> None:
        from core.autotrack import AutotrackManager

        item = self._at_list_widget.currentItem()
        if item is None:
            return
        list_id = int(item.data(0x0100))
        name, ok = QInputDialog.getText(self, _("Rename List"), _("New name:"), text=item.text())
        if ok and name.strip():
            AutotrackManager.rename_list(self._conn, list_id, name.strip())
            self._reload_at_lists()

    def _on_at_delete_list(self) -> None:
        from core.autotrack import AutotrackManager

        item = self._at_list_widget.currentItem()
        if item is None:
            return
        list_id = int(item.data(0x0100))
        ans = QMessageBox.question(
            self,
            _("Delete List"),
            _("Delete list '{name}' and all its entries?").format(name=item.text()),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            AutotrackManager.delete_list(self._conn, list_id)
            self._at_selected_list_id = None
            self._reload_at_lists()
            self._at_entry_tree.clear()

    def _on_at_add_entry(self) -> None:
        from core.autotrack import AutotrackManager

        if self._at_selected_list_id is None:
            QMessageBox.information(self, _("Autotrack"), _("Please select a list first."))
            return

        rows = self._conn.execute(
            "SELECT norad_cat_id, name FROM satellites WHERE is_hidden = 0 ORDER BY name"
        ).fetchall()
        if not rows:
            QMessageBox.information(self, _("Autotrack"), _("No satellites available."))
            return

        dlg = _SatSearchDialog(
            [(int(r["norad_cat_id"]), str(r["name"])) for r in rows], parent=self
        )
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.selected_norad is None:
            return
        norad = dlg.selected_norad

        xpdr_rows = self._conn.execute(
            "SELECT uuid, description, downlink_low, uplink_low, mode"
            " FROM transmitters WHERE norad_cat_id = ? AND alive = 1"
            " ORDER BY downlink_low",
            (norad,),
        ).fetchall()
        if not xpdr_rows:
            QMessageBox.information(
                self, _("Autotrack"), _("No transmitters found for this satellite.")
            )
            return

        def _xpdr_label(r: object) -> str:
            dl = (
                f"{int(r['downlink_low']) / 1e6:.3f}"  # type: ignore[index]
                if r["downlink_low"]  # type: ignore[index]
                else "?"
            )
            return f"{r['description']}  DL:{dl} MHz  {r['mode'] or ''}"  # type: ignore[index]

        xpdr_labels = [_xpdr_label(r) for r in xpdr_rows]
        xpdr_label, ok = QInputDialog.getItem(
            self, _("Select Transmitter"), _("Transmitter:"), xpdr_labels, 0, False
        )
        if not ok:
            return
        xpdr_idx = xpdr_labels.index(xpdr_label)
        xpdr_uuid = str(xpdr_rows[xpdr_idx]["uuid"])
        AutotrackManager.add_entry(self._conn, self._at_selected_list_id, norad, xpdr_uuid)
        self._reload_at_entries()

    def _on_at_remove_entry(self) -> None:
        from core.autotrack import AutotrackManager

        item = self._at_entry_tree.currentItem()
        if item is None:
            return
        raw = item.data(0, 0x0100)
        if not isinstance(raw, int):
            return
        AutotrackManager.remove_entry(self._conn, raw)
        self._reload_at_entries()

    def _on_at_move_up(self) -> None:
        from core.autotrack import AutotrackManager

        item = self._at_entry_tree.currentItem()
        if item is None:
            return
        raw = item.data(0, 0x0100)
        if isinstance(raw, int):
            AutotrackManager.move_entry_up(self._conn, raw)
        self._reload_at_entries()

    def _on_at_move_down(self) -> None:
        from core.autotrack import AutotrackManager

        item = self._at_entry_tree.currentItem()
        if item is None:
            return
        raw = item.data(0, 0x0100)
        if isinstance(raw, int):
            AutotrackManager.move_entry_down(self._conn, raw)
        self._reload_at_entries()
