"""Rig Settings > PTT tab: per-rig PTT method (CAT / RTS / DTR / VOX) and PTT port.

The chosen method and port are stored in each rig's own settings dict
(``ptt_method`` / ``ptt_port``); see rig.ptt for what each method does.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from i18n import _
from rig.ptt import (
    PTT_CAT,
    PTT_DTR,
    PTT_LINE_METHODS,
    PTT_RTS,
    PTT_VOX,
    SerialPttLine,
    normalize_ptt_method,
)

# How long the Test button keys the transmitter before releasing it again.
_TEST_KEY_MS = 1500


class _PttRigBox(QGroupBox):
    """PTT settings for one rig."""

    def __init__(
        self,
        title: str,
        scan_ports: Callable[[], list[str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(title, parent)
        self._scan_ports = scan_ports
        self._test_line: SerialPttLine | None = None
        self._test_timer = QTimer(self)
        self._test_timer.setSingleShot(True)
        self._test_timer.timeout.connect(self._finish_test)
        self._unavailable_reason = ""

        lay = QVBoxLayout(self)

        method_row = QHBoxLayout()
        method_row.addWidget(QLabel(_("PTT method:")))
        self._method_combo = QComboBox()
        self._method_combo.addItem(_("CAT"), PTT_CAT)
        self._method_combo.addItem(_("RTS"), PTT_RTS)
        self._method_combo.addItem(_("DTR"), PTT_DTR)
        self._method_combo.addItem(_("VOX"), PTT_VOX)
        self._method_combo.currentIndexChanged.connect(self._on_method_changed)
        method_row.addWidget(self._method_combo)
        method_row.addStretch()
        lay.addLayout(method_row)

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel(_("PTT port:")))
        self._port_combo = QComboBox()
        self._port_combo.setEditable(True)
        self._port_combo.setMinimumWidth(220)
        port_row.addWidget(self._port_combo)
        self._scan_btn = QPushButton(_("Scan"))
        self._scan_btn.clicked.connect(self._on_scan)
        port_row.addWidget(self._scan_btn)
        port_row.addStretch()
        lay.addLayout(port_row)

        test_row = QHBoxLayout()
        self._test_btn = QPushButton(_("Test PTT"))
        self._test_btn.clicked.connect(self._on_test)
        test_row.addWidget(self._test_btn)
        self._result_label = QLabel("")
        self._result_label.setWordWrap(True)
        test_row.addWidget(self._result_label, stretch=1)
        lay.addLayout(test_row)

        self._hint_label = QLabel("")
        self._hint_label.setWordWrap(True)
        self._hint_label.setStyleSheet("color: gray;")
        lay.addWidget(self._hint_label)

        self._on_method_changed()

    # -- settings --

    def load(self, method: Any, port: Any) -> None:
        idx = self._method_combo.findData(normalize_ptt_method(method))
        self._method_combo.setCurrentIndex(max(idx, 0))
        text = str(port or "")
        if text:
            i = self._port_combo.findText(text)
            if i >= 0:
                self._port_combo.setCurrentIndex(i)
            else:
                self._port_combo.setEditText(text)
        else:
            self._port_combo.setEditText("")

    def method(self) -> str:
        return str(self._method_combo.currentData())

    def port(self) -> str:
        return self._port_combo.currentText().strip()

    def set_unavailable(self, reason: str) -> None:
        """Grey the whole box out (SDR rig / disabled Rig 2); "" makes it usable again."""
        self._unavailable_reason = reason
        self._method_combo.setEnabled(not reason)
        self._on_method_changed()

    # -- UI state --

    def _on_method_changed(self) -> None:
        method = self.method()
        usable = not self._unavailable_reason
        is_line = method in PTT_LINE_METHODS
        self._port_combo.setEnabled(usable and is_line)
        self._scan_btn.setEnabled(usable and is_line)
        self._test_btn.setEnabled(usable and is_line and self._test_line is None)
        if not usable:
            self._test_btn.setToolTip(self._unavailable_reason)
            self._hint_label.setText(self._unavailable_reason)
            return
        if is_line:
            self._test_btn.setToolTip(_("Key the transmitter for about 1.5 seconds."))
            self._hint_label.setText(
                _(
                    "The radio must be set to accept RTS/DTR PTT (see its menu). "
                    "Direct mode: the port may be the same as the CAT port "
                    "(typical for Icom USB). NET mode: use a port that rigctld "
                    "is not using."
                )
            )
        elif method == PTT_VOX:
            self._test_btn.setToolTip(_("Not available: VOX sends no PTT signal."))
            self._hint_label.setText(
                _(
                    "The app sends no PTT signal. Enable VOX on the radio and set "
                    "its delay so the start and end of each transmission are not cut."
                )
            )
        else:
            self._test_btn.setToolTip(_("Not available: CAT PTT uses the connected rig."))
            self._hint_label.setText(
                _("PTT is sent over the rig's CAT/CI-V connection (the default).")
            )

    def _on_scan(self) -> None:
        current = self.port()
        ports = self._scan_ports()
        self._port_combo.clear()
        self._port_combo.addItems(ports)
        if current:
            i = self._port_combo.findText(current)
            if i >= 0:
                self._port_combo.setCurrentIndex(i)
            else:
                self._port_combo.setEditText(current)
        self._result_label.setText(
            _("{n} port(s) found").format(n=len(ports)) if ports else _("No serial ports found")
        )

    # -- test --

    def _on_test(self) -> None:
        method = self.method()
        port = self.port()
        if method not in PTT_LINE_METHODS or self._test_line is not None:
            return
        if not port:
            self._result_label.setText(_("Select the PTT port first."))
            return
        answer = QMessageBox.question(
            self,
            _("Test PTT"),
            _(
                "The transmitter will be keyed for about 1.5 seconds (an unmodulated "
                "carrier on the current frequency). Connect a dummy load or check "
                "that the antenna and frequency are safe to transmit on.\n\nContinue?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        line = SerialPttLine(port, method)
        if not line.open():
            self._result_label.setText(
                _("Cannot open {port} (in use by another program, or wrong port).").format(
                    port=port
                )
            )
            return
        if not line.key(True):
            line.close()
            self._result_label.setText(
                _("Could not set the {line} line.").format(line=method.upper())
            )
            return
        self._test_line = line
        self._test_btn.setEnabled(False)
        self._result_label.setText(_("Keying…"))
        self._test_timer.start(_TEST_KEY_MS)

    def _finish_test(self) -> None:
        self.release()
        self._result_label.setText(
            _("Test finished — the transmitter should have keyed and released.")
        )

    def release(self) -> None:
        """Drop the PTT line if a test is in progress (also on dialog close)."""
        self._test_timer.stop()
        line, self._test_line = self._test_line, None
        if line is not None:
            line.key(False)
            line.close()
        self._on_method_changed()


class PttPanel(QWidget):
    """The whole PTT tab: one box for Rig 1 and one for Rig 2."""

    def __init__(self, scan_ports: Callable[[], list[str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        self._boxes: dict[int, _PttRigBox] = {
            1: _PttRigBox(_("Rig 1"), scan_ports),
            2: _PttRigBox(_("Rig 2"), scan_ports),
        }
        lay.addWidget(self._boxes[1])
        lay.addWidget(self._boxes[2])
        lay.addStretch()

    def load(self, rig_index: int, settings: dict[str, Any]) -> None:
        self._boxes[rig_index].load(settings.get("ptt_method"), settings.get("ptt_port"))

    def save(self, rig_index: int) -> dict[str, str]:
        box = self._boxes[rig_index]
        return {"ptt_method": box.method(), "ptt_port": box.port()}

    def set_rig_state(self, rig_index: int, is_sdr: bool, enabled: bool) -> None:
        """Reflect the Rig tab: an SDR rig or a disabled Rig 2 has no PTT to configure."""
        if is_sdr:
            reason = _("This rig is an SDR: it has no transmitter to key.")
        elif not enabled:
            reason = _("Rig 2 is disabled (see the Rig 2 tab).")
        else:
            reason = ""
        self._boxes[rig_index].set_unavailable(reason)

    def release_all(self) -> None:
        for box in self._boxes.values():
            box.release()
