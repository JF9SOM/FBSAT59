"""PTT methods (CAT / RTS / DTR / VOX): controllers, serial line and the PTT tab."""

from __future__ import annotations

import socket
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytestqt.qtbot import QtBot

from rig.controller import HamlibDirectController, HamlibNetController, RigState
from rig.ptt import PTT_CAT, PTT_RTS, SerialPttLine, normalize_ptt_method
from ui.ptt_panel import PttPanel

# ---------------------------------------------------------------------------
# rig.ptt
# ---------------------------------------------------------------------------


def test_normalize_ptt_method_defaults_to_cat() -> None:
    assert normalize_ptt_method(None) == PTT_CAT
    assert normalize_ptt_method("") == PTT_CAT
    assert normalize_ptt_method("bogus") == PTT_CAT
    assert normalize_ptt_method(" RTS ") == PTT_RTS
    assert normalize_ptt_method("vox") == "vox"


def test_serial_line_opens_with_both_lines_low_and_keys_the_chosen_one() -> None:
    fake = MagicMock()
    with patch("serial.Serial", return_value=fake):
        line = SerialPttLine("/dev/cu.test", "dtr")
        assert line.open() is True
        # Lines are forced low before the port is opened.
        assert fake.rts is False and fake.dtr is False
        fake.open.assert_called_once()
        assert line.key(True) is True
        assert fake.dtr is True and fake.rts is False
        assert line.key(False) is True
        assert fake.dtr is False


def test_serial_line_close_drops_the_line_first() -> None:
    fake = MagicMock()
    with patch("serial.Serial", return_value=fake):
        line = SerialPttLine("/dev/cu.test", "rts")
        line.open()
        line.key(True)
        line.close()
    assert fake.rts is False
    fake.close.assert_called_once()
    assert line.is_open is False
    assert line.key(True) is False  # closed: cannot key


def test_serial_line_open_failure_is_reported_not_raised() -> None:
    with patch("serial.Serial", side_effect=OSError("busy")):
        line = SerialPttLine("/dev/cu.test", "rts")
        assert line.open() is False
    assert line.key(True) is False


def test_serial_line_rejects_non_line_method() -> None:
    with pytest.raises(ValueError):
        SerialPttLine("/dev/cu.test", "cat")


# ---------------------------------------------------------------------------
# NET controller
# ---------------------------------------------------------------------------


def _net(method: str, port: str = "/dev/cu.ptt") -> HamlibNetController:
    ctrl = HamlibNetController(host="localhost", port=4532, ctcss_method="ft991")
    ctrl.set_ptt_config(method, port)
    sock = MagicMock(spec=socket.socket)
    sock.recv.return_value = b"RPRT 0\n"
    ctrl._sock = sock
    with ctrl._lock:
        ctrl._state = RigState.CONNECTED
    return ctrl


def test_net_rts_keys_the_serial_line_not_rigctld() -> None:
    ctrl = _net("rts")
    line = MagicMock()
    line.key.return_value = True
    ctrl._ptt_line = line
    ctrl._sock.sendall.reset_mock()  # type: ignore[union-attr]

    assert ctrl.set_ptt(True, freeze_doppler=False) is True
    line.key.assert_called_with(True)
    assert ctrl.set_ptt(False) is True
    line.key.assert_called_with(False)
    ctrl._sock.sendall.assert_not_called()  # type: ignore[union-attr]


def test_net_line_ptt_off_needs_no_rigctld_even_when_the_socket_is_gone() -> None:
    ctrl = _net("dtr")
    line = MagicMock()
    line.key.return_value = True
    ctrl._ptt_line = line
    ctrl.set_ptt(True, freeze_doppler=False)
    ctrl._sock = None
    with ctrl._lock:
        ctrl._state = RigState.DISCONNECTED
    with patch("rig.controller.socket.socket") as sock_cls:
        assert ctrl.set_ptt(False) is True
    sock_cls.assert_not_called()
    line.key.assert_called_with(False)


def test_net_line_ptt_on_fails_when_the_port_could_not_be_opened() -> None:
    ctrl = _net("rts")
    ctrl._ptt_line = None
    assert ctrl.set_ptt(True, freeze_doppler=False) is False
    assert ctrl._ptt_active is False  # flags rolled back


def test_net_line_ptt_off_retries_then_reports_failure() -> None:
    ctrl = _net("rts")
    line = MagicMock()
    line.key.side_effect = [True, False, False, False]
    ctrl._ptt_line = line
    ctrl.set_ptt(True, freeze_doppler=False)
    with patch("rig.controller.time.sleep"):
        assert ctrl.set_ptt(False) is False
    assert line.key.call_count == 4  # 1 key-up + 3 attempts to drop it


def test_net_vox_sends_nothing() -> None:
    ctrl = _net("vox")
    ctrl._sock.sendall.reset_mock()  # type: ignore[union-attr]
    assert ctrl.set_ptt(True, freeze_doppler=False) is True
    assert ctrl.set_ptt(False) is True
    ctrl._sock.sendall.assert_not_called()  # type: ignore[union-attr]


def test_net_cat_still_uses_rigctld() -> None:
    ctrl = _net("cat")
    ctrl._sock.sendall.reset_mock()  # type: ignore[union-attr]
    assert ctrl.set_ptt(True, freeze_doppler=False) is True
    assert ctrl._sock.sendall.call_args.args[0] == b"T 1\n"  # type: ignore[union-attr]


def test_net_connect_opens_and_disconnect_closes_the_ptt_line() -> None:
    ctrl = HamlibNetController(host="localhost", port=4532)
    ctrl.set_ptt_config("rts", "/dev/cu.ptt")
    line = MagicMock()
    line.open.return_value = True
    sock = MagicMock(spec=socket.socket)
    sock.recv.return_value = b"RPRT 0\n"
    with (
        patch("rig.controller.socket.socket", return_value=sock),
        patch("rig.controller.SerialPttLine", return_value=line) as line_cls,
    ):
        assert ctrl.connect() is True
        line_cls.assert_called_once_with("/dev/cu.ptt", "rts")
        ctrl.disconnect()
    line.close.assert_called()


def test_net_line_loss_of_the_command_socket_does_not_unkey_a_line_rig() -> None:
    ctrl = _net("rts")
    ctrl._ptt_line = MagicMock()
    ctrl._ptt_line.key.return_value = True
    ctrl.set_ptt(True, freeze_doppler=False)
    ctrl._sock.sendall.side_effect = TimeoutError("timed out")  # type: ignore[union-attr]
    with patch.object(ctrl, "_emergency_ptt_off_async") as emergency, ctrl._cmd_lock:
        ctrl._cmd_raw("F 435600000")
    emergency.assert_not_called()  # the line is independent of rigctld


# ---------------------------------------------------------------------------
# Direct controller
# ---------------------------------------------------------------------------


def _direct(method: str, port: str = "/dev/cu.ptt") -> HamlibDirectController:
    ctrl = HamlibDirectController(model_id=3081, port="/dev/cu.cat")
    ctrl.set_ptt_config(method, port)
    ctrl._rig = MagicMock(error_status=0)
    ctrl._hamlib = MagicMock()
    with ctrl._lock:
        ctrl._state = RigState.CONNECTED
    return ctrl


def test_direct_vox_sends_nothing() -> None:
    ctrl = _direct("vox")
    assert ctrl.set_ptt(True) is True
    assert ctrl.set_ptt(False) is True
    ctrl._rig.set_ptt.assert_not_called()


def test_direct_rts_still_goes_through_hamlib_set_ptt() -> None:
    ctrl = _direct("rts")
    assert ctrl.set_ptt(True) is True
    ctrl._rig.set_ptt.assert_called_once()


@pytest.mark.parametrize(("method", "hamlib_name"), [("rts", "RTS"), ("dtr", "DTR")])
def test_direct_connect_configures_hamlib_ptt(method: str, hamlib_name: str) -> None:
    fake_hamlib = MagicMock()
    rig = MagicMock(error_status=0)
    fake_hamlib.Rig.return_value = rig
    ctrl = HamlibDirectController(model_id=3081, port="/dev/cu.cat")
    ctrl.set_ptt_config(method, "/dev/cu.cat")  # same port as CAT: Icom USB
    with (
        patch("rig.controller.HAMLIB_AVAILABLE", True),
        patch.dict(sys.modules, {"Hamlib": fake_hamlib}),
        patch("rig.controller._open_rig_with_retry"),
        patch.object(HamlibDirectController, "_init_split"),
    ):
        assert ctrl.connect() is True
    confs = {c.args[0]: c.args[1] for c in rig.set_conf.call_args_list}
    assert confs["ptt_type"] == hamlib_name
    assert confs["ptt_pathname"] == "/dev/cu.cat"


def test_direct_connect_without_ptt_port_fails_clearly() -> None:
    fake_hamlib = MagicMock()
    ctrl = HamlibDirectController(model_id=3081, port="/dev/cu.cat")
    ctrl.set_ptt_config("rts", "")
    with (
        patch("rig.controller.HAMLIB_AVAILABLE", True),
        patch.dict(sys.modules, {"Hamlib": fake_hamlib}),
    ):
        assert ctrl.connect() is False
    assert ctrl.state == RigState.ERROR


def test_direct_cat_does_not_touch_hamlib_ptt_settings() -> None:
    fake_hamlib = MagicMock()
    rig = MagicMock(error_status=0)
    fake_hamlib.Rig.return_value = rig
    ctrl = HamlibDirectController(model_id=3081, port="/dev/cu.cat")
    with (
        patch("rig.controller.HAMLIB_AVAILABLE", True),
        patch.dict(sys.modules, {"Hamlib": fake_hamlib}),
        patch("rig.controller._open_rig_with_retry"),
        patch.object(HamlibDirectController, "_init_split"),
    ):
        ctrl.connect()
    assert "ptt_type" not in {c.args[0] for c in rig.set_conf.call_args_list}


# ---------------------------------------------------------------------------
# PTT tab
# ---------------------------------------------------------------------------


def _panel(qtbot: QtBot) -> PttPanel:
    panel = PttPanel(lambda: ["/dev/cu.a", "/dev/cu.b"])
    qtbot.addWidget(panel)
    return panel


def test_panel_round_trips_each_rigs_method_and_port(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    panel.load(1, {"ptt_method": "rts", "ptt_port": "/dev/cu.a"})
    panel.load(2, {"ptt_method": "vox"})
    assert panel.save(1) == {"ptt_method": "rts", "ptt_port": "/dev/cu.a"}
    assert panel.save(2) == {"ptt_method": "vox", "ptt_port": ""}


def test_panel_defaults_to_cat_for_old_settings(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    panel.load(1, {"mode": "net", "host": "localhost"})
    assert panel.save(1)["ptt_method"] == "cat"


def test_panel_port_and_test_are_only_enabled_for_rts_dtr(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    box: Any = panel._boxes[1]
    for method, enabled in (("cat", False), ("rts", True), ("dtr", True), ("vox", False)):
        panel.load(1, {"ptt_method": method, "ptt_port": "/dev/cu.a"})
        assert box._port_combo.isEnabled() is enabled
        assert box._test_btn.isEnabled() is enabled


def test_panel_greys_out_sdr_and_disabled_rigs(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    panel.load(2, {"ptt_method": "rts", "ptt_port": "/dev/cu.a"})
    panel.set_rig_state(2, is_sdr=False, enabled=False)
    box: Any = panel._boxes[2]
    assert not box._method_combo.isEnabled()
    assert not box._test_btn.isEnabled()
    panel.set_rig_state(2, is_sdr=True, enabled=True)
    assert not box._method_combo.isEnabled()
    panel.set_rig_state(2, is_sdr=False, enabled=True)
    assert box._method_combo.isEnabled()
    assert box._test_btn.isEnabled()


def test_panel_test_keys_then_releases(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    panel.load(1, {"ptt_method": "rts", "ptt_port": "/dev/cu.a"})
    box: Any = panel._boxes[1]
    line = MagicMock()
    line.open.return_value = True
    line.key.return_value = True
    with (
        patch("ui.ptt_panel.QMessageBox.question", return_value=_yes()),
        patch("ui.ptt_panel.SerialPttLine", return_value=line),
    ):
        box._on_test()
        line.key.assert_called_with(True)
        assert not box._test_btn.isEnabled()  # no second test while keyed
        box._finish_test()
    line.key.assert_called_with(False)
    line.close.assert_called_once()
    assert box._test_btn.isEnabled()


def test_panel_release_all_drops_a_running_test(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    panel.load(1, {"ptt_method": "dtr", "ptt_port": "/dev/cu.a"})
    box: Any = panel._boxes[1]
    line = MagicMock()
    line.open.return_value = True
    line.key.return_value = True
    with (
        patch("ui.ptt_panel.QMessageBox.question", return_value=_yes()),
        patch("ui.ptt_panel.SerialPttLine", return_value=line),
    ):
        box._on_test()
    panel.release_all()  # what closing the dialog does
    line.key.assert_called_with(False)
    line.close.assert_called_once()


def test_panel_test_declined_does_not_open_the_port(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    panel.load(1, {"ptt_method": "rts", "ptt_port": "/dev/cu.a"})
    box: Any = panel._boxes[1]
    with (
        patch("ui.ptt_panel.QMessageBox.question", return_value=_no()),
        patch("ui.ptt_panel.SerialPttLine") as line_cls,
    ):
        box._on_test()
    line_cls.assert_not_called()


def test_panel_test_without_port_asks_for_one(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    panel.load(1, {"ptt_method": "rts", "ptt_port": ""})
    box: Any = panel._boxes[1]
    with patch("ui.ptt_panel.QMessageBox.question") as ask:
        box._on_test()
    ask.assert_not_called()


def _yes() -> Any:
    from PySide6.QtWidgets import QMessageBox

    return QMessageBox.StandardButton.Yes


def _no() -> Any:
    from PySide6.QtWidgets import QMessageBox

    return QMessageBox.StandardButton.No


# ---------------------------------------------------------------------------
# Rig Settings dialog integration
# ---------------------------------------------------------------------------


def _dialog_conn(rig1: dict[str, Any], rig2: dict[str, Any]) -> Any:
    import json
    import sqlite3

    from data.database import SCHEMA_SQL

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    for key, val in (("rig1_settings", rig1), ("rig2_settings", rig2)):
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (key, json.dumps(val)),
        )
    conn.commit()
    return conn


def _saved(conn: Any, key: str) -> dict[str, Any]:
    import json

    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return dict(json.loads(row["value"]))


def test_dialog_saves_ptt_into_each_rigs_own_settings(qtbot: QtBot) -> None:
    from ui.rig_dialog import RigSettingsDialog

    conn = _dialog_conn(
        {"mode": "direct", "model_id": 3081, "ptt_method": "rts", "ptt_port": "/dev/cu.a"},
        {"mode": "net", "enabled": True, "ptt_method": "vox"},
    )
    dlg = RigSettingsDialog(conn)
    qtbot.addWidget(dlg)
    assert dlg._tabs.tabText(4) == "PTT"

    dlg._ptt_panel.load(1, {"ptt_method": "dtr", "ptt_port": "/dev/cu.b"})
    dlg._save_settings()

    s1 = _saved(conn, "rig1_settings")
    s2 = _saved(conn, "rig2_settings")
    assert (s1["ptt_method"], s1["ptt_port"]) == ("dtr", "/dev/cu.b")
    assert (s2["ptt_method"], s2["ptt_port"]) == ("vox", "")
    assert s1["mode"] == "direct" and s1["model_id"] == 3081  # rig settings untouched


def test_dialog_old_settings_without_ptt_default_to_cat(qtbot: QtBot) -> None:
    from ui.rig_dialog import RigSettingsDialog

    conn = _dialog_conn({"mode": "net", "host": "localhost"}, {"enabled": False})
    dlg = RigSettingsDialog(conn)
    qtbot.addWidget(dlg)
    dlg._save_settings()
    assert _saved(conn, "rig1_settings")["ptt_method"] == "cat"
    assert not dlg._ptt_panel._boxes[2]._method_combo.isEnabled()  # Rig 2 disabled


def test_net_ptt_on_swaps_in_a_freshly_opened_line_and_closes_the_old_one() -> None:
    ctrl = _net("dtr")
    old = MagicMock()
    old.key.return_value = True
    ctrl._ptt_line = old
    fresh = MagicMock()
    fresh.open.return_value = True
    fresh.key.return_value = True
    with patch("rig.controller.SerialPttLine", return_value=fresh):
        assert ctrl.set_ptt(True, freeze_doppler=False) is True
    fresh.key.assert_called_with(True)
    old.close.assert_called_once()
    old.key.assert_not_called()
    assert ctrl._ptt_line is fresh


def test_net_ptt_on_keeps_the_old_line_when_the_fresh_open_fails() -> None:
    ctrl = _net("dtr")
    old = MagicMock()
    old.key.return_value = True
    ctrl._ptt_line = old
    fresh = MagicMock()
    fresh.open.return_value = False
    with patch("rig.controller.SerialPttLine", return_value=fresh):
        assert ctrl.set_ptt(True, freeze_doppler=False) is True
    old.key.assert_called_with(True)
    old.close.assert_not_called()
    assert ctrl._ptt_line is old


# ---------------------------------------------------------------------------
# After a transmission: wait for the rig to answer CAT, restore a stray mode
# ---------------------------------------------------------------------------


def test_slot_write_waits_for_the_rig_to_answer_after_a_transmission() -> None:
    ctrl = _net("dtr")
    ctrl._ptt_line = MagicMock()
    ctrl._ptt_line.key.return_value = True
    ctrl.set_ptt(True, freeze_doppler=False)
    ctrl.set_ptt(False)
    ctrl._ptt_off_at = float("-inf")  # past the fixed settle time
    assert ctrl._cat_probe_due is True
    answers = iter([False, True])
    with (
        patch.object(ctrl, "_cat_answers", side_effect=lambda: next(answers)),
        patch("rig.controller.time.sleep"),
        patch.object(ctrl, "_cmd_raw", return_value="RPRT 0"),
    ):
        assert ctrl.write_dl_hz(435_610_000.0) is True
    assert ctrl._cat_probe_due is False


def test_slot_write_is_skipped_when_the_rig_never_answers() -> None:
    ctrl = _net("dtr")
    ctrl._cat_probe_due = True
    with (
        patch.object(ctrl, "_cat_answers", return_value=False),
        patch.object(ctrl, "_cmd_raw") as cmd,
    ):
        assert ctrl.write_dl_hz(435_610_000.0, still_ok=lambda: False) is False
    cmd.assert_not_called()


def test_restore_rx_mode_sends_the_data_mode_of_the_transponder() -> None:
    ctrl = _net("dtr")
    ctrl.set_current_modes("USB-D", "LSB-D")
    ctrl._ptt_off_at = float("-inf")
    sock = MagicMock()
    with (
        patch("rig.controller.socket.socket", return_value=sock),
        patch.object(ctrl, "_ft991_read_mode", return_value="PKTUSB"),
        patch("rig.controller.time.sleep"),
    ):
        assert ctrl.restore_rx_mode() == "PKTUSB"
    sock.sendall.assert_called_with(b"w MD0C;\n")


def test_restore_rx_mode_does_nothing_for_a_non_data_transponder() -> None:
    ctrl = _net("dtr")
    ctrl.set_current_modes("FM", "FM")
    assert ctrl.restore_rx_mode() is None


def test_waiting_for_the_rig_to_answer_gives_up_quickly() -> None:
    """A deaf rig must not hold the CAT job for the whole receive slot (uplink write starved)."""
    ctrl = _net("dtr")
    ctrl._cat_probe_due = True
    clock = {"t": 1000.0}

    def fake_monotonic() -> float:
        return clock["t"]

    def fake_sleep(s: float) -> None:
        clock["t"] += s

    with (
        patch.object(ctrl, "_cat_answers", return_value=False) as probe,
        patch("rig.controller.time.monotonic", side_effect=fake_monotonic),
        patch("rig.controller.time.sleep", side_effect=fake_sleep),
    ):
        assert ctrl._await_cat_answer(still_ok=lambda: True) is False
    assert clock["t"] - 1000.0 <= 2.0
    assert probe.call_count <= 8
