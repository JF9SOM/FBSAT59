"""Tests for core/ai_help.py (Help > AI Help… prompt building) and its dialog."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from PySide6.QtGui import QGuiApplication
from pytestqt.qtbot import QtBot

from core.ai_help import (
    MAX_URL_LENGTH,
    HelpEnvironment,
    IncludeOptions,
    build_claude_url,
    build_prompt,
    environment_text,
    guide_urls,
)
from ui.ai_help_dialog import AiHelpDialog

ENV = HelpEnvironment(
    app_version="0.3.61",
    os_name="Darwin 25.6.0",
    language="ja",
    devices=("Rig 1: IC-9700", "Rotator: connected"),
)


def test_guide_urls_follow_language() -> None:
    ja = guide_urls("ja")
    assert len(ja) == 3
    assert all("/user-guide/ja/" in u for u in ja)
    assert all("/user-guide/en/" in u for u in guide_urls("en"))
    assert all("/user-guide/en/" in u for u in guide_urls("fr"))


def test_environment_text_respects_options() -> None:
    full = environment_text(ENV, IncludeOptions())
    assert "v0.3.61" in full and "Darwin" in full and "IC-9700" in full and "ja" in full
    none = environment_text(
        ENV, IncludeOptions(version=False, os_name=False, language=False, devices=False)
    )
    assert none == ""
    assert "IC-9700" not in environment_text(ENV, IncludeOptions(devices=False))


def test_prompt_contains_guide_rules_and_question() -> None:
    prompt = build_prompt("Doppler が効かない", "FBSAT59 v1", "ja")
    assert "/user-guide/ja/troubleshooting.md" in prompt
    assert "Reply in Japanese" in prompt
    assert "Do not guess" in prompt
    assert "Doppler が効かない" in prompt
    assert "FBSAT59 v1" in prompt


def test_prompt_without_question_or_environment() -> None:
    prompt = build_prompt("  ", "", "en")
    assert "(not shared)" in prompt
    assert "please ask me" in prompt


def test_prompt_never_mentions_private_data() -> None:
    prompt = build_prompt("q", environment_text(ENV, IncludeOptions()), "en").lower()
    assert "qth" not in prompt.replace("never ask for passwords, callsign or location", "")


def test_claude_url_roundtrips_prompt() -> None:
    url, truncated = build_claude_url("日本語の質問", "env", "ja")
    assert not truncated
    assert url.startswith("https://claude.ai/new?q=")
    decoded = parse_qs(urlparse(url).query)["q"][0]
    assert decoded == build_prompt("日本語の質問", "env", "ja")


def test_claude_url_truncates_only_the_question() -> None:
    url, truncated = build_claude_url("あ" * 5000, "env", "ja")
    assert truncated
    assert len(url) <= MAX_URL_LENGTH
    decoded = parse_qs(urlparse(url).query)["q"][0]
    assert "/user-guide/ja/index.md" in decoded and "Do not guess" in decoded


def test_dialog_preview_and_copy(qtbot: QtBot) -> None:
    dlg = AiHelpDialog(ENV)
    qtbot.addWidget(dlg)
    dlg._question_edit.setPlainText("rig does not connect")
    assert "rig does not connect" in dlg.prompt_text()
    assert "IC-9700" in dlg._preview.toPlainText()
    dlg._devices_cb.setChecked(False)
    assert "IC-9700" not in dlg._preview.toPlainText()
    dlg._on_copy()
    clip = QGuiApplication.clipboard()
    assert clip is not None
    assert clip.text() == dlg.prompt_text()
