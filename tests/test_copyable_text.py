"""Tests for ui/copyable_text.py — selectable command labels + Copy/Run buttons.

Uses conftest.py's offscreen Qt platform and pytest-qt's ``qtbot`` fixture
(see tests/test_rig_dialog_sdr.py for why qtbot.addWidget() matters here).
"""

from __future__ import annotations

from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from ui.copyable_text import CommandRow, make_copy_button, make_run_button, strip_html


def test_strip_html_converts_br_and_block_tags_to_newlines() -> None:
    html = "Ubuntu / Debian<br>sudo apt install gnuradio<br><br>after</p>next"
    assert strip_html(html) == "Ubuntu / Debian\nsudo apt install gnuradio\n\nafter\nnext"


def test_strip_html_drops_inline_tags_without_adding_whitespace() -> None:
    assert strip_html("<code>brew install foo</code>") == "brew install foo"


def test_make_copy_button_copies_plain_text_to_clipboard(qtbot: QtBot) -> None:
    btn = make_copy_button(lambda: "<code>brew install foo</code>")
    qtbot.addWidget(btn)
    btn.click()
    assert QApplication.clipboard().text() == "brew install foo"


def test_make_run_button_invokes_terminal_launcher(qtbot: QtBot) -> None:
    btn = make_run_button(lambda: "<code>brew install foo</code>")
    qtbot.addWidget(btn)
    with patch("ui.copyable_text.open_terminal_and_run", return_value=(True, "")) as mock_open:
        btn.click()
    mock_open.assert_called_once_with("brew install foo")


def test_make_run_button_shows_error_on_failure(qtbot: QtBot) -> None:
    btn = make_run_button(lambda: "echo hi")
    qtbot.addWidget(btn)
    with patch(
        "ui.copyable_text.open_terminal_and_run",
        return_value=(False, "no terminal found"),
    ):
        btn.click()
    assert "no terminal found" in btn.toolTip()


class TestCommandRow:
    def test_label_is_mouse_selectable(self, qtbot: QtBot) -> None:
        row = CommandRow("<code>brew install foo</code>")
        qtbot.addWidget(row)
        flags = row._label.textInteractionFlags()
        assert flags & Qt.TextInteractionFlag.TextSelectableByMouse

    def test_copy_button_present_and_copies_plain_text(self, qtbot: QtBot) -> None:
        row = CommandRow("<code>brew install foo</code>")
        qtbot.addWidget(row)
        assert not row._copy_btn.isHidden()
        row._copy_btn.click()
        assert QApplication.clipboard().text() == "brew install foo"

    def test_run_button_hidden_by_default(self, qtbot: QtBot) -> None:
        row = CommandRow("brew install foo")
        qtbot.addWidget(row)
        assert row._run_btn is None

    def test_run_button_shown_when_allow_run(self, qtbot: QtBot) -> None:
        row = CommandRow("brew install foo", allow_run=True)
        qtbot.addWidget(row)
        assert row._run_btn is not None
        assert not row._run_btn.isHidden()
        with patch("ui.copyable_text.open_terminal_and_run", return_value=(True, "")) as mock_open:
            row._run_btn.click()
        mock_open.assert_called_once_with("brew install foo")

    def test_empty_text_hides_both_buttons(self, qtbot: QtBot) -> None:
        row = CommandRow("brew install foo", allow_run=True)
        qtbot.addWidget(row)
        row.setText("")
        assert row._copy_btn.isHidden()
        assert row._run_btn is not None
        assert row._run_btn.isHidden()

    def test_set_text_updates_label(self, qtbot: QtBot) -> None:
        row = CommandRow()
        qtbot.addWidget(row)
        row.setText("brew install bar")
        assert row.text() == "brew install bar"
