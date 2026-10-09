"""Help > AI Help… dialog.

Lets the user open claude.ai with a pre-filled prompt that points Claude at
the end-user guide (``docs/user-guide/``), or copy that prompt to the
clipboard. Nothing is sent anywhere until the user presses a button, and the
exact text that will be used is always shown in the preview.
"""

from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.ai_help import (
    HelpEnvironment,
    IncludeOptions,
    build_claude_url,
    build_prompt,
    environment_text,
)
from i18n import _


class AiHelpDialog(QDialog):
    """Compose the AI Help prompt and hand it to claude.ai or the clipboard."""

    def __init__(self, env: HelpEnvironment, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._env = env
        self.setWindowTitle(_("AI Help"))
        self.setMinimumSize(620, 560)
        self._build_ui()
        self._refresh_preview()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        intro = QLabel(
            _(
                "Ask Claude about FBSAT59. Your browser opens claude.ai with a prompt that "
                "points Claude at the FBSAT59 user guide on GitHub. You need a Claude account; "
                "if Claude cannot read the guide, turn on web search in claude.ai. "
                "Nothing is sent until you press send in claude.ai."
            )
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(QLabel(_("Your question (optional):")))
        self._question_edit = QPlainTextEdit()
        self._question_edit.setPlaceholderText(
            _("e.g. My rig does not follow Doppler. What should I check?")
        )
        self._question_edit.setMaximumHeight(90)
        self._question_edit.textChanged.connect(self._refresh_preview)
        layout.addWidget(self._question_edit)

        share_box = QGroupBox(_("Information to attach"))
        share_layout = QVBoxLayout(share_box)
        self._version_cb = QCheckBox(_("App version"))
        self._os_cb = QCheckBox(_("Operating system"))
        self._lang_cb = QCheckBox(_("Interface language"))
        self._devices_cb = QCheckBox(_("Connected rig / SDR types"))
        for cb in (self._version_cb, self._os_cb, self._lang_cb, self._devices_cb):
            cb.setChecked(True)
            cb.toggled.connect(self._refresh_preview)
            share_layout.addWidget(cb)
        note = QLabel(_("Log files, your location, callsign and IP address are never included."))
        note.setWordWrap(True)
        share_layout.addWidget(note)
        layout.addWidget(share_box)

        layout.addWidget(QLabel(_("Prompt that will be used:")))
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        layout.addWidget(self._preview, 1)

        self._warn_label = QLabel("")
        self._warn_label.setWordWrap(True)
        self._warn_label.setStyleSheet("color: #e67e22;")
        layout.addWidget(self._warn_label)

        row = QHBoxLayout()
        self._open_btn = QPushButton(_("Open in Claude"))
        self._open_btn.clicked.connect(self._on_open)
        self._copy_btn = QPushButton(_("Copy Prompt"))
        self._copy_btn.setToolTip(
            _("Copy the prompt, then paste it into claude.ai or the Claude app yourself.")
        )
        self._copy_btn.clicked.connect(self._on_copy)
        close_btn = QPushButton(_("Close"))
        close_btn.clicked.connect(self.accept)
        row.addWidget(self._open_btn)
        row.addWidget(self._copy_btn)
        row.addStretch()
        row.addWidget(close_btn)
        layout.addLayout(row)

    # ------------------------------------------------------------------
    # Prompt composition
    # ------------------------------------------------------------------

    def _include_options(self) -> IncludeOptions:
        return IncludeOptions(
            version=self._version_cb.isChecked(),
            os_name=self._os_cb.isChecked(),
            language=self._lang_cb.isChecked(),
            devices=self._devices_cb.isChecked(),
        )

    def _env_text(self) -> str:
        return environment_text(self._env, self._include_options())

    def prompt_text(self) -> str:
        """Return the full prompt (also what 'Copy Prompt' puts on the clipboard)."""
        return build_prompt(self._question_edit.toPlainText(), self._env_text(), self._env.language)

    def _refresh_preview(self) -> None:
        self._preview.setPlainText(self.prompt_text())
        _url, truncated = build_claude_url(
            self._question_edit.toPlainText(), self._env_text(), self._env.language
        )
        self._warn_label.setText(
            _(
                "Your question is too long for the link and will be shortened. "
                "Use Copy Prompt to keep it whole."
            )
            if truncated
            else ""
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_open(self) -> None:
        url, _truncated = build_claude_url(
            self._question_edit.toPlainText(), self._env_text(), self._env.language
        )
        QDesktopServices.openUrl(QUrl(url))

    def _on_copy(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.prompt_text())
        self._copy_btn.setText(_("Copied!"))
