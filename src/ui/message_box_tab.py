"""Communications > Message Box/Digipeater tab.

One tab for the satellites that store and relay short messages. A protocol
combo switches between the existing AX100 panel (MARMOTSat/GreenCube
digipeaters, ``Ax100DigiTab``, unchanged) and the ARICA-2 message box
(``Arica2Panel``). Both panels stay alive while the other is shown; only the
visible one's input is meant to be used, so switching stops the hidden panel's
receive path.

MainWindow's duck-typed hooks (``set_use_utc``, ``refresh_sdr_pipeline``) are
forwarded to both panels.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QStackedWidget, QVBoxLayout, QWidget

from i18n import _
from ui.arica2_panel import Arica2Panel
from ui.ax100_digi_tab import Ax100DigiTab

_SETTINGS_KEY = "message_box_tab_settings"
_PROTOCOL_AX100 = "ax100"
_PROTOCOL_ARICA2 = "arica2"
# Catalog numbers the Quick Panel's satellite request is made with (MARMOTSat's
# real, visible id; ARICA-2).
_NORAD_BY_PROTOCOL = {_PROTOCOL_AX100: 69912, _PROTOCOL_ARICA2: 68796}
_TAB_KEY = "ax100digi"


class MessageBoxTab(QWidget):
    """Protocol selector over the AX100 digipeater and ARICA-2 panels."""

    # (tab_key, norad): MainWindow selects the satellite and its matching
    # transponder, same as the Comms Quick Panel's satellite combo.
    satellite_requested: Signal = Signal(str, int)

    def __init__(
        self,
        conn: sqlite3.Connection,
        radio_control: QWidget | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._conn = conn

        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel(_("Satellite / protocol:")))
        self._protocol_combo = QComboBox()
        self._protocol_combo.addItem(_("AX100 digipeater (MARMOTSat)"), _PROTOCOL_AX100)
        self._protocol_combo.addItem(_("ARICA-2 message box"), _PROTOCOL_ARICA2)
        row.addWidget(self._protocol_combo)
        row.addStretch(1)
        layout.addLayout(row)

        self._stack = QStackedWidget()
        self._ax100 = Ax100DigiTab(conn, radio_control, parent=self)
        self._arica2 = Arica2Panel(conn, radio_control, parent=self, autostart=False)
        self._stack.addWidget(self._ax100)
        self._stack.addWidget(self._arica2)
        layout.addWidget(self._stack)

        saved = self._load_protocol()
        self._protocol_combo.setCurrentIndex(max(0, self._protocol_combo.findData(saved)))
        self._protocol_combo.currentIndexChanged.connect(self._on_protocol_changed)
        self._apply_protocol()

    # ------------------------------------------------------------------ #

    def _load_protocol(self) -> str:
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (_SETTINGS_KEY,)
        ).fetchone()
        if not row:
            return _PROTOCOL_AX100
        try:
            return str(json.loads(row[0]).get("protocol", _PROTOCOL_AX100))
        except (ValueError, AttributeError):
            return _PROTOCOL_AX100

    def _save_protocol(self) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (_SETTINGS_KEY, json.dumps({"protocol": self._protocol_combo.currentData()})),
        )
        self._conn.commit()

    def current_protocol(self) -> str:
        """The selected protocol key (``"ax100"`` or ``"arica2"``)."""
        return str(self._protocol_combo.currentData())

    @Slot(int)
    def _on_protocol_changed(self, _index: int) -> None:
        self._apply_protocol()
        self._save_protocol()
        self.request_satellite()

    def request_satellite(self) -> None:
        """Ask MainWindow to select the current protocol's satellite/transponder."""
        self.satellite_requested.emit(_TAB_KEY, _NORAD_BY_PROTOCOL[self.current_protocol()])

    def _apply_protocol(self) -> None:
        """Show the chosen panel and move the input to it.

        The panels must not both hold the audio/SDR input, so the one being
        hidden is released first and the shown one (re)starts its own.
        """
        if self.current_protocol() == _PROTOCOL_ARICA2:
            self._stack.setCurrentWidget(self._arica2)
            self._ax100._disconnect_sdr_audio()
            self._ax100._disconnect_soundcard_audio()
            self._arica2.start_input()
        else:
            self._stack.setCurrentWidget(self._ax100)
            self._arica2.stop_input()
            self._ax100._disconnect_sdr_audio()
            self._ax100._disconnect_soundcard_audio()
            self._ax100._apply_input_source()

    # ------------------------------------------------------------------ #
    # MainWindow duck-typed hooks
    # ------------------------------------------------------------------ #

    def set_use_utc(self, use_utc: bool) -> None:
        """Forward View > Time Zone changes to both panels."""
        self._ax100.set_use_utc(use_utc)
        self._arica2.set_use_utc(use_utc)

    def refresh_sdr_pipeline(self, pipeline: Any) -> None:
        """Forward SDR (re)connects to the panel that is showing."""
        if self.current_protocol() == _PROTOCOL_ARICA2:
            self._arica2.refresh_sdr_pipeline(pipeline)
        else:
            self._ax100.refresh_sdr_pipeline(pipeline)

    def closeEvent(self, event: Any) -> None:
        self._arica2.shutdown()
        self._ax100.close()
        super().closeEvent(event)
