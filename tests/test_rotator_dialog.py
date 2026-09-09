"""Unit tests for ui/rotator_dialog.py — Direct-mode connection Test button.

Verifies the Baud Rate row's "Test" button: when it is shown/hidden, that
port/baud/model changes reset it, that _probe_rotator() keys off
rot.error_status, and that its result drives the button green/red.

Uses conftest.py's offscreen Qt platform and pytest-qt's ``qtbot`` fixture
with ``qtbot.addWidget()`` (not a manual QApplication + .close()) — see
test_rig_dialog_sdr.py for why that teardown path is a segfault hazard for
these dialog widgets.
"""

from __future__ import annotations

import sys
import types

import pytest
from pytestqt.qtbot import QtBot

import ui.rotator_dialog as rotator_dialog
from ui.rotator_dialog import RotatorSettingsDialog, _probe_rotator

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

    seen: list[tuple[int, str, int]] = []

    def _fake_probe(model_id: int, port: str, baud: int) -> bool:
        seen.append((model_id, port, baud))
        return ok

    monkeypatch.setattr(rotator_dialog, "_probe_rotator", _fake_probe)

    dlg._on_baud_test()
    qtbot.waitUntil(lambda: dlg._baud_test_btn.text() == expected_text, timeout=3000)
    assert expected_color in dlg._baud_test_btn.styleSheet()
    assert dlg._baud_test_btn.isEnabled()
    assert seen == [(_GS232A, "COM7", 9600)]


class _FakeRot:
    """Minimal stand-in for a Hamlib.Rot object."""

    def __init__(self, model_id: int, *, err_after_open: int) -> None:
        self.model_id = model_id
        self._err_after_open = err_after_open
        self.error_status = 0
        self.conf: dict[str, str] = {}
        self.closed = False

    def set_conf(self, key: str, value: str) -> None:
        self.conf[key] = value

    def open(self) -> None:
        self.error_status = self._err_after_open

    def close(self) -> None:
        self.closed = True


def _install_fake_hamlib(monkeypatch: pytest.MonkeyPatch, *, err_after_open: int) -> list[_FakeRot]:
    created: list[_FakeRot] = []

    def _rot(model_id: int) -> _FakeRot:
        r = _FakeRot(model_id, err_after_open=err_after_open)
        created.append(r)
        return r

    fake = types.ModuleType("Hamlib")
    fake.Rot = _rot  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "Hamlib", fake)
    return created


def test_probe_rotator_true_when_error_status_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _install_fake_hamlib(monkeypatch, err_after_open=0)
    assert _probe_rotator(_GS232A, "COM7", 9600) is True
    assert created[0].conf == {"rot_pathname": "COM7", "serial_speed": "9600"}
    assert created[0].closed


def test_probe_rotator_false_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # -5 == -RIG_ETIMEOUT: what a wrong baud produces on the SkyWatcher.
    created = _install_fake_hamlib(monkeypatch, err_after_open=-5)
    assert _probe_rotator(_GS232A, "COM7", 4800) is False
    assert created[0].closed


def test_probe_rotator_false_when_hamlib_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "Hamlib", None)
    assert _probe_rotator(_GS232A, "COM7", 9600) is False
