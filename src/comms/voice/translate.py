"""Text translation for the Digital Voice tab, through MyMemory's public API.

MyMemory (api.mymemory.translated.net) is a documented web API that needs no key. Its free
tier allows about 5000 characters a day per IP address and 500 characters per request.
(Google Translate's key-less web endpoint was tried first and does not work for an app: it
answers 429 with a bot-check page to this kind of client.) The text sent is the
speech-recognition result of a message (public amateur-radio content), nothing else.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

_URL = "https://api.mymemory.translated.net/get"
_MAX_CHARS = 450  # the service accepts at most 500 characters per request


class RateLimitedError(Exception):
    """The free daily quota is used up (or the service answers 429)."""


# (language code, name in its own language) offered as translation targets.
LANGUAGES: tuple[tuple[str, str], ...] = (
    ("ja", "日本語"),
    ("en", "English"),
    ("es", "Español"),
    ("fr", "Français"),
    ("de", "Deutsch"),
    ("pt", "Português"),
    ("it", "Italiano"),
    ("zh-CN", "简体中文"),
    ("ko", "한국어"),
    ("ru", "Русский"),
)


def chunks(text: str, limit: int = _MAX_CHARS) -> list[str]:
    """Split *text* into pieces of at most *limit* characters, at sentence ends if possible."""
    pieces: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?。！？])\s+", text.strip()):
        while len(sentence) > limit:  # a sentence longer than the limit: cut it
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        if current and len(current) + 1 + len(sentence) > limit:
            pieces.append(current)
            current = ""
        current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def parse_response(data: Any) -> tuple[str, str]:
    """``(detected source language, translated text)`` from the service's JSON.

    Raises ``RateLimitedError`` for the daily quota, ``ValueError`` for any other error.
    """
    status = int(data.get("responseStatus", 0))
    text = str(data["responseData"]["translatedText"])
    if status == 429 or "MYMEMORY WARNING" in text.upper():
        raise RateLimitedError(text)
    if status != 200:
        raise ValueError(text or f"translation service error {status}")
    detected = str(data["responseData"].get("detectedLanguage") or "")
    return detected, text


def translate(
    text: str, target: str, source: str = "auto", timeout: float = 15.0
) -> tuple[str, str]:
    """Translate *text* into *target*; returns ``(detected source language, translation)``.

    *source* ``"auto"`` lets the service detect the language (more reliable than the code
    speech recognition guessed). Raises ``RateLimitedError`` (daily quota),
    ``httpx.HTTPError`` (network, HTTP status) or ``ValueError`` (unexpected reply).
    """
    pair = f"{'Autodetect' if source == 'auto' else source}|{target}"
    detected = ""
    parts: list[str] = []
    with httpx.Client(timeout=timeout) as client:
        for piece in chunks(text):
            response = client.get(_URL, params={"q": piece, "langpair": pair})
            if response.status_code == 429:
                raise RateLimitedError("translation service limit reached")
            response.raise_for_status()
            try:
                found, translated = parse_response(response.json())
            except (KeyError, TypeError, AttributeError) as exc:
                raise ValueError("unexpected reply from the translation service") from exc
            detected = detected or found
            parts.append(translated)
    return detected, " ".join(parts)
