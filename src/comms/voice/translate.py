"""Text translation for the Digital Voice tab, through Google Translate's free web endpoint.

There is no API key: ``translate.googleapis.com/translate_a/single?client=gtx`` is the
endpoint behind the Google Translate web page. It is not an official API, so Google may
change or limit it. The text sent is the speech-recognition result of a message (public
amateur-radio content), nothing else.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

_URL = "https://translate.googleapis.com/translate_a/single"
_MAX_CHARS = 1500  # one request carries at most this much text

# (Google language code, name in its own language) offered as translation targets.
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
    """``(detected source language, translated text)`` from the endpoint's JSON."""
    translated = "".join(seg[0] for seg in data[0] if seg and seg[0])
    detected = data[2] if len(data) > 2 and isinstance(data[2], str) else ""
    return detected, translated


def translate(
    text: str, target: str, source: str = "auto", timeout: float = 15.0
) -> tuple[str, str]:
    """Translate *text* into *target*; returns ``(detected source language, translation)``.

    Raises ``httpx.HTTPError`` (network, HTTP status) or ``ValueError`` (unexpected reply).
    """
    detected = ""
    parts: list[str] = []
    with httpx.Client(timeout=timeout) as client:
        for piece in chunks(text):
            response = client.get(
                _URL,
                params={"client": "gtx", "sl": source, "tl": target, "dt": "t", "q": piece},
            )
            response.raise_for_status()
            try:
                found, translated = parse_response(response.json())
            except (IndexError, KeyError, TypeError, ValueError) as exc:
                raise ValueError("unexpected reply from the translation service") from exc
            detected = detected or found
            parts.append(translated)
    return detected, " ".join(parts)
