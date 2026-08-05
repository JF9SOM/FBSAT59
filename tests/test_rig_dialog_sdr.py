"""Unit tests for ui/rig_dialog.py — SoapyRemote "Add Remote Host…" support.

Verifies the save()/load() round-trip of manually-added remote SDR hosts,
that they survive re-enumeration of real hardware, and that the Remove
button is only enabled for entries the user actually added (not for real
hardware or for LAN-broadcast-discovered remote devices).

Uses conftest.py's offscreen Qt platform and pytest-qt's ``qtbot`` fixture
(rather than a manually-managed QApplication + explicit .close()) — no real
SoapySDR installation or network access required, since SdrDeviceInfo is a
plain dataclass and SOAPY_AVAILABLE is monkeypatched to exercise the panel's
full UI branch.

``qtbot.addWidget()`` matters here, not just style: constructing these
widgets with a manually-managed QApplication and tearing them down via a
bare ``.close()`` was found to segfault the interpreter at process exit
(reproducible, if intermittently) — a pre-existing Qt object lifetime
hazard in _SdrSettingsPanel/_AddRemoteHostDialog that had never been
exercised by a test before this file existed. qtbot.addWidget() defers
deletion correctly and avoids it; do not revert to manual app/close().

_SdrSettingsPanel._start_enumerate() is stubbed out in the panel fixture:
in real use it spawns a background thread that calls the (here, genuinely
absent) SoapySDR and emits a cross-thread Qt signal back to the panel —
safe under a running QApplication.exec() event loop, but these tests never
enter one, so a signal delivered after a test function returns is another
possible source of teardown-time races unrelated to what these tests are
meant to exercise.
"""

from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot

import ui.rig_dialog as rig_dialog
from sdr.device import SdrDeviceInfo


@pytest.fixture
def panel(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> rig_dialog._SdrSettingsPanel:
    monkeypatch.setattr(rig_dialog, "SOAPY_AVAILABLE", True)
    monkeypatch.setattr(
        rig_dialog._SdrSettingsPanel, "_start_enumerate", lambda self, force=False: None
    )
    p = rig_dialog._SdrSettingsPanel()
    qtbot.addWidget(p)
    return p


def test_load_restores_saved_remote_host(panel: rig_dialog._SdrSettingsPanel) -> None:
    panel.load(
        {
            "remote_hosts": [
                {
                    "host": "192.168.1.50",
                    "port": "55132",
                    "driver_hint": "rtlsdr",
                    "label": "Shed SDR",
                }
            ]
        }
    )
    assert panel._dev_combo.count() == 1
    assert panel._dev_combo.itemText(0) == "Shed SDR"
    assert panel._devices[0].args == {
        "driver": "remote",
        "remote": "192.168.1.50:55132",
        "remote:driver": "rtlsdr",
    }


def test_save_round_trips_remote_hosts(panel: rig_dialog._SdrSettingsPanel) -> None:
    entry = {"host": "10.0.0.5", "port": "55132", "driver_hint": "", "label": ""}
    panel._remote_hosts.append(entry)
    panel._rebuild_combo()
    saved = panel.save()
    assert saved["remote_hosts"] == [entry]
    assert saved["device_args"] == {"driver": "remote", "remote": "10.0.0.5:55132"}


def test_remote_host_survives_empty_hw_reenumerate(panel: rig_dialog._SdrSettingsPanel) -> None:
    panel.load(
        {"remote_hosts": [{"host": "192.168.1.50", "port": "", "driver_hint": "", "label": ""}]}
    )
    panel._on_enumerate([])  # real hardware enumerate finds nothing
    assert len(panel._devices) == 1
    assert panel._devices[0].driver == "remote"


def test_remote_host_merges_with_real_hardware(panel: rig_dialog._SdrSettingsPanel) -> None:
    panel.load(
        {"remote_hosts": [{"host": "192.168.1.50", "port": "", "driver_hint": "", "label": ""}]}
    )
    hw = SdrDeviceInfo(
        driver="rtlsdr", label="RTL-SDR", serial="1234", hardware="", args={"driver": "rtlsdr"}
    )
    panel._on_enumerate([hw])
    drivers = [d.driver for d in panel._devices]
    assert drivers == ["rtlsdr", "remote"]


def test_remove_button_only_enabled_for_saved_remote_entries(
    panel: rig_dialog._SdrSettingsPanel,
) -> None:
    panel.load(
        {"remote_hosts": [{"host": "192.168.1.50", "port": "", "driver_hint": "", "label": ""}]}
    )
    hw = SdrDeviceInfo(
        driver="rtlsdr", label="RTL-SDR", serial="1234", hardware="", args={"driver": "rtlsdr"}
    )
    panel._on_enumerate([hw])

    panel._dev_combo.setCurrentIndex(0)  # real hardware entry
    assert panel._remove_remote_btn.isEnabled() is False

    panel._dev_combo.setCurrentIndex(1)  # saved remote entry
    assert panel._remove_remote_btn.isEnabled() is True


def test_remove_deletes_only_the_selected_remote_host(
    panel: rig_dialog._SdrSettingsPanel,
) -> None:
    panel.load(
        {
            "remote_hosts": [
                {"host": "192.168.1.50", "port": "", "driver_hint": "", "label": "Shed"},
                {"host": "10.0.0.5", "port": "", "driver_hint": "", "label": "Garage"},
            ]
        }
    )
    assert panel._dev_combo.count() == 2
    panel._dev_combo.setCurrentIndex(0)  # "Shed"
    panel._on_remove_remote_host()
    assert len(panel._remote_hosts) == 1
    assert panel._remote_hosts[0]["label"] == "Garage"


def test_add_remote_host_dialog_rejects_blank_host(qtbot: QtBot) -> None:
    dlg = rig_dialog._AddRemoteHostDialog()
    qtbot.addWidget(dlg)
    dlg._host_edit.setText("   ")
    dlg._on_accept()
    assert dlg.result() == 0  # still open / not accepted


def test_add_remote_host_dialog_result_entry(qtbot: QtBot) -> None:
    dlg = rig_dialog._AddRemoteHostDialog()
    qtbot.addWidget(dlg)
    dlg._host_edit.setText("shed.local")
    dlg._port_spin.setValue(12345)
    dlg._driver_edit.setText("hackrf")
    dlg._label_edit.setText("Backyard Shed")
    assert dlg.result_entry() == {
        "host": "shed.local",
        "port": "12345",
        "driver_hint": "hackrf",
        "label": "Backyard Shed",
    }


def _serial_row_labels(panel: rig_dialog._SdrSettingsPanel) -> list[str]:
    """Return the form-row label texts of the panel's SDR Device group."""
    from PySide6.QtWidgets import QFormLayout

    texts: list[str] = []
    for form in panel.findChildren(QFormLayout):
        for row in range(form.rowCount()):
            item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            widget = item.widget() if item is not None else None
            if widget is not None:
                texts.append(widget.text())
    return texts


def test_serial_row_shown_off_windows(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rig_dialog, "SOAPY_AVAILABLE", True)
    monkeypatch.setattr(
        rig_dialog._SdrSettingsPanel, "_start_enumerate", lambda self, force=False: None
    )
    monkeypatch.setattr(rig_dialog.sys, "platform", "linux")
    panel = rig_dialog._SdrSettingsPanel()
    qtbot.addWidget(panel)
    assert "Serial:" in _serial_row_labels(panel)


def test_serial_row_hidden_on_windows(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows never populates Serial (ctypes bypass), so the row is omitted.

    A permanently blank "Serial: —" there looked like a malfunction and was
    reported as one by users.
    """
    monkeypatch.setattr(rig_dialog, "SOAPY_AVAILABLE", True)
    monkeypatch.setattr(
        rig_dialog._SdrSettingsPanel, "_start_enumerate", lambda self, force=False: None
    )
    monkeypatch.setattr(rig_dialog.sys, "platform", "win32")
    panel = rig_dialog._SdrSettingsPanel()
    qtbot.addWidget(panel)
    labels = _serial_row_labels(panel)
    assert "Serial:" not in labels
    # Driver: must still be there — only Serial goes away.
    assert "Driver:" in labels
    # The label object still exists so _on_device_selected() needs no guard.
    panel._on_device_selected(0)
