"""Tests for ui/gr_satellites_dialog.py.

Uses conftest.py's offscreen Qt platform and pytest-qt's ``qtbot`` fixture
(see tests/test_rig_dialog_sdr.py for why qtbot.addWidget() matters here).
No real gr-satellites/GNU Radio/network required.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from pytestqt.qtbot import QtBot

import ui.gr_satellites_dialog as mod


def test_manual_instructions_never_mention_pip_install_gr_satellites() -> None:
    # gr-satellites is not published on PyPI (pypi.org/simple/gr-satellites/
    # returns 404) — this string must never reappear in the guidance text.
    for plat in ("linux", "darwin", "win32", "other"):
        with patch.object(mod.sys, "platform", plat):
            html, _primary_cmd = mod._get_instructions()
        assert "pip install gr-satellites" not in html


def test_detect_returns_not_installed_when_nothing_found() -> None:
    with patch.object(mod, "find_gr_satellites_executable", return_value=None):
        installed, detail, bundled = mod._detect_gr_satellites()
    assert installed is False
    assert bundled is False
    assert detail == ""


def test_detect_reports_bundled_with_version() -> None:
    bundled_path = Path("/fake/gr-satellites-env/bin/gr_satellites")
    with (
        patch.object(mod, "find_gr_satellites_executable", return_value=(bundled_path, True)),
        patch.object(mod, "bundled_version", return_value="5.10.0"),
    ):
        installed, detail, bundled = mod._detect_gr_satellites()
    assert installed is True
    assert bundled is True
    assert "5.10.0" in detail


def test_detect_reports_system_install() -> None:
    system_path = Path("/usr/bin/gr_satellites")
    with patch.object(mod, "find_gr_satellites_executable", return_value=(system_path, False)):
        installed, detail, bundled = mod._detect_gr_satellites()
    assert installed is True
    assert bundled is False
    assert str(system_path) in detail


class TestDialogConstruction:
    def test_constructs_without_bundle_installed(self, qtbot: QtBot) -> None:
        with patch.object(mod, "find_gr_satellites_executable", return_value=None):
            dlg = mod.GrSatellitesDialog()
            qtbot.addWidget(dlg)
        assert "NOT installed" in dlg._status_lbl.text()
        assert dlg._btn_uninstall.isHidden() is True

    def test_constructs_with_bundle_installed(self, qtbot: QtBot) -> None:
        bundled_path = Path("/fake/gr-satellites-env/bin/gr_satellites")
        with (
            patch.object(mod, "find_gr_satellites_executable", return_value=(bundled_path, True)),
            patch.object(mod, "bundled_version", return_value="5.10.0"),
            patch.object(mod, "is_bundle_installed", return_value=True),
        ):
            dlg = mod.GrSatellitesDialog()
            qtbot.addWidget(dlg)
        assert "5.10.0" in dlg._status_lbl.text()
        assert dlg._btn_uninstall.isHidden() is False

    def test_uninstall_button_calls_uninstall_bundle(self, qtbot: QtBot) -> None:
        with patch.object(mod, "find_gr_satellites_executable", return_value=None):
            dlg = mod.GrSatellitesDialog()
            qtbot.addWidget(dlg)

        with (
            patch.object(
                mod.QMessageBox, "question", return_value=mod.QMessageBox.StandardButton.Yes
            ),
            patch.object(mod, "uninstall_bundle") as mock_uninstall,
        ):
            dlg._on_uninstall()

        mock_uninstall.assert_called_once()
