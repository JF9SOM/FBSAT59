"""Prompt building for Help > AI Help….

Builds the text that is pre-filled into claude.ai (or copied to the clipboard)
so that Claude answers from the end-user guide in ``docs/user-guide/``.

This module is deliberately free of Qt and network access: it only turns
plain data into strings, which keeps it easy to unit-test. Nothing is sent
anywhere by this module; the dialog opens the URL only when the user presses
the button.

Privacy: only the fields of :class:`HelpEnvironment` are ever included. Log
files, location (QTH), callsign and IP address are never part of the prompt.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass, field
from urllib.parse import quote

GUIDE_BASE_URL = "https://raw.githubusercontent.com/JF9SOM/fbsat59/main/docs/user-guide"
GUIDE_FILES: tuple[str, ...] = ("index.md", "getting-started.md", "ft4.md", "troubleshooting.md")
ISSUES_URL = "https://github.com/JF9SOM/fbsat59/issues"
CLAUDE_NEW_URL = "https://claude.ai/new"

# Upper bound for the whole claude.ai URL (after percent-encoding). Browsers
# accept far longer URLs, but long query strings are fragile in some setups.
MAX_URL_LENGTH = 8000

_LANGUAGE_NAMES = {"ja": "Japanese", "en": "English"}


@dataclass(frozen=True)
class HelpEnvironment:
    """Non-sensitive facts about the running app that may be attached."""

    app_version: str = ""
    os_name: str = ""
    language: str = "en"
    devices: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class IncludeOptions:
    """Which parts of :class:`HelpEnvironment` the user agreed to attach."""

    version: bool = True
    os_name: bool = True
    language: bool = True
    devices: bool = True


def collect_os_name() -> str:
    """Return a short OS description such as ``Darwin 25.6.0``."""
    return f"{platform.system()} {platform.release()}".strip()


def guide_language(language: str) -> str:
    """Return the user-guide sub-folder for ``language`` (``ja`` or ``en``)."""
    return "ja" if language == "ja" else "en"


def guide_urls(language: str) -> list[str]:
    """Return the raw GitHub URLs of the user-guide pages for ``language``."""
    folder = guide_language(language)
    return [f"{GUIDE_BASE_URL}/{folder}/{name}" for name in GUIDE_FILES]


def environment_text(env: HelpEnvironment, include: IncludeOptions) -> str:
    """Return the one-line environment summary, or "" if nothing is attached."""
    parts: list[str] = []
    if include.version and env.app_version:
        parts.append(f"FBSAT59 v{env.app_version}")
    if include.os_name and env.os_name:
        parts.append(env.os_name)
    if include.language and env.language:
        parts.append(f"UI language: {env.language}")
    if include.devices and env.devices:
        parts.append("connected: " + ", ".join(env.devices))
    return "; ".join(parts)


def build_prompt(question: str, env_text: str, language: str) -> str:
    """Return the full prompt text for Claude.

    The prompt is written in English (compact once percent-encoded); the
    user's own question is inserted verbatim. Claude is asked to reply in the
    app's UI language.
    """
    reply_language = _LANGUAGE_NAMES.get(language, "English")
    urls = "\n".join(f"- {u}" for u in guide_urls(language))
    question_text = question.strip() or "(none yet - please ask me what I need help with)"
    env_line = env_text or "(not shared)"
    return (
        "You are the support assistant for FBSAT59, satellite tracking and "
        "communications software for radio amateurs.\n"
        "First read these user-guide pages. They are your only source of truth "
        "(web search/fetch may need to be enabled):\n"
        f"{urls}\n"
        "Rules:\n"
        "- Answer only from the guide. If it is not covered, say you do not know "
        f"and suggest opening an issue at {ISSUES_URL} . Do not guess.\n"
        "- Do not rely on CLAUDE.md or developer documents.\n"
        f"- Reply in {reply_language}, in short numbered steps, using the menu "
        "names shown in the app.\n"
        "- If the problem is still not solved at the end, write a GitHub Issue "
        "draft I can paste (title, environment, steps, expected vs actual, what "
        "I tried). Never ask for passwords, callsign or location.\n"
        f"My environment: {env_line}\n"
        f"My question: {question_text}"
    )


def build_claude_url(question: str, env_text: str, language: str) -> tuple[str, bool]:
    """Return ``(url, truncated)`` for ``claude.ai/new?q=<prompt>``.

    ``truncated`` is True when the question had to be shortened to keep the
    URL within :data:`MAX_URL_LENGTH`; only the question is ever shortened.
    """
    question = question.strip()
    truncated = False
    while True:
        prompt = build_prompt(question, env_text, language)
        url = f"{CLAUDE_NEW_URL}?q={quote(prompt, safe='')}"
        if len(url) <= MAX_URL_LENGTH or not question:
            return url, truncated
        truncated = True
        question = question[: max(0, int(len(question) * 0.8) - 1)]
