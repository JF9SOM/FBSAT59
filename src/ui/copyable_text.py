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


class CommandRow(QWidget):
    """A mouse-selectable command label with an inline Copy button.

    ``text`` may contain simple HTML (e.g. ``<code>...</code>``); HTML tags
    are stripped before copying to the clipboard. The Copy button hides
    itself automatically when the text is empty.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._label = QLabel()
        self._label.setWordWrap(True)
        make_selectable(self._label)
        self._copy_btn = make_copy_button(self._label.text, self)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._copy_btn, 0, Qt.AlignmentFlag.AlignTop)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._label.setText(text)
        self._copy_btn.setVisible(bool(strip_html(text)))
        self._copy_btn.setText(_("📋 Copy"))

    def text(self) -> str:
        return self._label.text()
