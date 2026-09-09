"""Unit tests for ui/rotator_dialog.py — Direct-mode connection Test button.

Verifies the Baud Rate row's "Test" button: when it is shown/hidden, that
port/baud/model changes reset it, and that a mocked HamlibRotatorController
connect() drives it green (success) or red (failure).

Uses conftest.py's offscreen Qt platform and pytest-qt's ``qtbot`` fixture
with ``qtbot.addWidget()`` (not a manual QApplication + .close()) — see
test_rig_dialog_sdr.py for why that teardown path is a segfault hazard for
these dialog widgets.
"""

from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot

import rig.controller as rig_controller
import ui.rotator_dialog as rotator_dialog
from ui.rotator_dialog import RotatorSettingsDialog

_GS232A = 601  # a real serial rotator model — testable
_GS232_GENERIC = 602  # another real serial rotator model
_DUMMY = 1  # Hamlib pseudo-model — not testable
_NET = 2  # NET rotctl pseudo-model — not testable


def _make_dialog(qtbot: QtBot) -> RotatorSettingsDialog:
    # object() has no .execute, so _load_settings() is a no-op (no DB needed).
    dlg = RotatorSettingsDialog(object())
    qtbot.addWidget(dlg)
    return dlg


def _select_model(dlg: RotatorSettingsDialog, model_id: int) -> None:
    for i in range(dlg._model_combo.count()):
        if dlg._model_combo.itemData(i) == model_id:
            dlg._model_combo.setCurrentIndex(i)
            return
    raise AssertionError(f"model {model_id} not in combo")


@pytest.fixture(autouse=True)
def _hamlib_available(monkeypatch: pytest.MonkeyPatch) -> None:
    # Visibility of the Test button keys off HAMLIB_AVAILABLE; force it on so
    # the tests are deterministic on CI (where Hamlib is absent).
    monkeypatch.setattr(rotator_dialog, "HAMLIB_AVAILABLE", True)


def test_test_button_exists_and_neutral(qtbot: QtBot) -> None:
    dlg = _make_dialog(qtbot)
    assert dlg._baud_test_btn.text() == "Test"
    assert dlg._baud_test_btn.styleSheet() == ""


def test_hidden_for_pseudo_models(qtbot: QtBot) -> None:
    dlg = _make_dialog(qtbot)
    _select_model(dlg, _DUMMY)
    assert not dlg._baud_test_btn.isVisibleTo(dlg)
    _select_model(dlg, _NET)
    assert not dlg._baud_test_btn.isVisibleTo(dlg)


def test_visible_for_real_serial_model(qtbot: QtBot) -> None:
    dlg = _make_dialog(qtbot)
    _select_model(dlg, _GS232A)
    assert dlg._baud_test_btn.isVisibleTo(dlg)


def test_hidden_without_hamlib(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    dlg = _make_dialog(qtbot)
    _select_model(dlg, _GS232A)
    monkeypatch.setattr(rotator_dialog, "HAMLIB_AVAILABLE", False)
    dlg._update_baud_test_visibility()
    assert not dlg._baud_test_btn.isVisibleTo(dlg)


def test_hidden_in_net_mode(qtbot: QtBot) -> None:
    dlg = _make_dialog(qtbot)
    _select_model(dlg, _GS232A)
    dlg._radio_net.setChecked(True)
    assert not dlg._baud_test_btn.isVisibleTo(dlg)


def test_model_change_resets_button(qtbot: QtBot) -> None:
    dlg = _make_dialog(qtbot)
    _select_model(dlg, _GS232A)
    dlg._baud_test_btn.setText("✓ OK")
    dlg._baud_test_btn.setStyleSheet("background-color: #27ae60;")
    _select_model(dlg, _GS232_GENERIC)
    assert dlg._baud_test_btn.text() == "Test"
    assert dlg._baud_test_btn.styleSheet() == ""


def test_baud_change_resets_button(qtbot: QtBot) -> None:
    dlg = _make_dialog(qtbot)
    _select_model(dlg, _GS232A)
    dlg._baud_test_btn.setText("✗ Failed")
    dlg._baud_combo.setCurrentText("19200")
    assert dlg._baud_test_btn.text() == "Test"


def test_empty_port_shows_no_port(qtbot: QtBot) -> None:
    dlg = _make_dialog(qtbot)
    _select_model(dlg, _GS232A)
    dlg._port_combo.setEditText("")
    dlg._on_baud_test()
    assert dlg._baud_test_btn.text() == "No port"
    assert "orange" in dlg._baud_test_btn.styleSheet()


class _FakeRotator:
    def __init__(self, *, ok: bool) -> None:
        self._ok = ok
        self.disconnected = False

    def connect(self) -> bool:
        return self._ok

    def disconnect(self) -> None:
        self.disconnected = True


@pytest.mark.parametrize(
    ("ok", "expected_text", "expected_color"),
    [(True, "✓ OK", "#27ae60"), (False, "✗ Failed", "#c0392b")],
)
def test_baud_test_result_drives_button(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    ok: bool,
    expected_text: str,
    expected_color: str,
) -> None:
    dlg = _make_dialog(qtbot)
    _select_model(dlg, _GS232A)
    dlg._port_combo.setEditText("COM7")

    created: list[_FakeRotator] = []

    def _factory(*, model_id: int, port: str, baud_rate: int) -> _FakeRotator:
        assert (model_id, port, baud_rate) == (_GS232A, "COM7", 9600)
        r = _FakeRotator(ok=ok)
        created.append(r)
        return r

    # _on_baud_test() does `from rig.controller import HamlibRotatorController`
    # inside its worker, so patch it on the source module.
    monkeypatch.setattr(rig_controller, "HamlibRotatorController", _factory)

    dlg._on_baud_test()
    qtbot.waitUntil(lambda: dlg._baud_test_btn.text() == expected_text, timeout=3000)
    assert expected_color in dlg._baud_test_btn.styleSheet()
    assert dlg._baud_test_btn.isEnabled()
    assert created and created[0].disconnected
