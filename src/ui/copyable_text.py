"""Reusable UI helpers for copy-pasteable install commands in Help dialogs.

QLabel does not allow mouse text selection by default, so long shell commands
shown in Help dialogs (Homebrew bootstrap, apt/pip/git-clone build recipes,
etc.) could not be selected or copied on any platform. These helpers add both
mouse-selectable text and an explicit Copy button wherever such commands are
displayed.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QWidget

from core.terminal_launcher import open_terminal_and_run
from i18n import _

_SELECTABLE_FLAGS = (
    Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
)


def make_selectable(label: QLabel) -> None:
    """Allow the user to select (and copy) a QLabel's displayed text with the mouse."""
    label.setTextInteractionFlags(label.textInteractionFlags() | _SELECTABLE_FLAGS)
    label.setCursor(Qt.CursorShape.IBeamCursor)


def strip_html(text: str) -> str:
    """Convert simple rich-text markup to plain text, for copying to the clipboard.

    ``<br>`` and block-level tags become newlines before the remaining tags are
    dropped, so multi-line install instructions don't collapse onto one line.
    """
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|pre|li)\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def make_copy_button(get_text: Callable[[], str], parent: QWidget | None = None) -> QPushButton:
    """Create a small button that copies ``get_text()`` (HTML stripped) to the clipboard."""
    btn = QPushButton(_("📋 Copy"), parent)

    def _on_click() -> None:
        QApplication.clipboard().setText(strip_html(get_text()))
        btn.setText(_("Copied!"))

    btn.clicked.connect(_on_click)
    return btn


def make_run_button(get_text: Callable[[], str], parent: QWidget | None = None) -> QPushButton:
    """Create a button that opens a terminal and runs ``get_text()`` (HTML stripped).

    The command runs interactively in a real, visible terminal window rather
    than a hidden subprocess, so the user can see the output, respond to a
    sudo password prompt, or cancel with Ctrl+C.
    """
    btn = QPushButton(_("▶ Run in Terminal"), parent)

    def _on_click() -> None:
        success, error = open_terminal_and_run(strip_html(get_text()))
        if success:
            btn.setText(_("Opened Terminal"))
        else:
            btn.setText(_("Failed — use Copy instead"))
            btn.setToolTip(error)

    btn.clicked.connect(_on_click)
    return btn


class CommandRow(QWidget):
    """A mouse-selectable command label with inline Copy and Run buttons.

    ``text`` may contain simple HTML (e.g. ``<code>...</code>``); HTML tags
    are stripped before copying/running. Both buttons hide themselves
    automatically when the text is empty. Pass ``allow_run=True`` only for
    text that is a single, self-contained, safe-to-execute command — not for
    reference text that mixes prose with multiple alternative commands.
    """

    def __init__(
        self, text: str = "", parent: QWidget | None = None, allow_run: bool = False
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._label = QLabel()
        self._label.setWordWrap(True)
        make_selectable(self._label)
        self._copy_btn = make_copy_button(self._label.text, self)
        self._run_btn = make_run_button(self._label.text, self) if allow_run else None
        layout.addWidget(self._label, 1)
        layout.addWidget(self._copy_btn, 0, Qt.AlignmentFlag.AlignTop)
        if self._run_btn is not None:
            layout.addWidget(self._run_btn, 0, Qt.AlignmentFlag.AlignTop)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._label.setText(text)
        has_text = bool(strip_html(text))
        self._copy_btn.setVisible(has_text)
        self._copy_btn.setText(_("📋 Copy"))
        if self._run_btn is not None:
            self._run_btn.setVisible(has_text)
            self._run_btn.setText(_("▶ Run in Terminal"))

    def text(self) -> str:
        return self._label.text()
