"""Unit tests for ui/rig_dialog.py — SoapyRemote "Add Remote Host…" support.

Verifies the save()/load() round-trip of manually-added remote SDR hosts,
that they survive re-enumeration of real hardware, and that the Remove
button is only enabled for entries the user actually added (not for real
hardware or for LAN-broadcast-discovered remote devices).

Uses conftest.py's offscreen Qt platform — no real SoapySDR installation
or network access required, since SdrDeviceInfo is a plain dataclass and
SOAPY_AVAILABLE is monkeypatched to exercise the panel's full UI branch.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

import ui.rig_dialog as rig_dialog
from sdr.device import SdrDeviceInfo


@pytest.fixture
def app() -> QApplication:
    existing = QApplication.instance()
    return existing if isinstance(existing, QApplication) else QApplication([])


@pytest.fixture
def panel(app: QApplication, monkeypatch: pytest.MonkeyPatch) -> rig_dialog._SdrSettingsPanel:
    monkeypatch.setattr(rig_dialog, "SOAPY_AVAILABLE", True)
    p = rig_dialog._SdrSettingsPanel()
    yield p
    p.close()


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


def test_add_remote_host_dialog_rejects_blank_host(app: QApplication) -> None:
    dlg = rig_dialog._AddRemoteHostDialog()
    try:
        dlg._host_edit.setText("   ")
        dlg._on_accept()
        assert dlg.result() == 0  # still open / not accepted
    finally:
        dlg.close()


def test_add_remote_host_dialog_result_entry(app: QApplication) -> None:
    dlg = rig_dialog._AddRemoteHostDialog()
    try:
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
    finally:
        dlg.close()
