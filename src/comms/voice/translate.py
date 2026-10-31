"""Text translation for the Digital Voice tab, through Google Translate's free web endpoint.

There is no API key: ``translate.googleapis.com/translate_a/single?client=gtx`` is the
endpoint behind the Google Translate web page. It is not an official API, so Google may
change or limit it. The text sent is the speech-recognition result of a message (public
amateur-radio content), nothing else.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

import httpx

_URL = "https://translate.googleapis.com/translate_a/single"
_MAX_CHARS = 1500  # one request carries at most this much text
# Waits (s) before each retry after "429 Too Many Requests"; the endpoint limits requests per
# IP address and the limit lifts after a short while.
_RETRY_WAITS = (4.0, 10.0)


class RateLimitedError(Exception):
    """The service kept answering 429 Too Many Requests, even after the retries."""


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


def _get(
    client: httpx.Client, params: dict[str, str], sleep: Callable[[float], None]
) -> httpx.Response:
    """GET one request; after a 429 wait and try again (the waits of ``_RETRY_WAITS``)."""
    for wait in (*_RETRY_WAITS, None):
        response = client.get(_URL, params=params)
        if response.status_code != 429:
            response.raise_for_status()
            return response
        if wait is None:
            break
        sleep(wait)
    raise RateLimitedError(
        "Google Translate is limiting requests from this connection; try again in a while"
    )


def translate(
    text: str,
    target: str,
    source: str = "auto",
    timeout: float = 15.0,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, str]:
    """Translate *text* into *target*; returns ``(detected source language, translation)``.

    Raises ``RateLimitedError`` (429 even after retries), ``httpx.HTTPError`` (network, other
    HTTP status) or ``ValueError`` (unexpected reply). *client* and *sleep* are for tests.
    """
    detected = ""
    parts: list[str] = []
    own = client is None
    http = client if client is not None else httpx.Client(timeout=timeout)
    try:
        for piece in chunks(text):
            params = {"client": "gtx", "sl": source, "tl": target, "dt": "t", "q": piece}
            response = _get(http, params, sleep)
            try:
                found, translated = parse_response(response.json())
            except (IndexError, KeyError, TypeError, ValueError) as exc:
                raise ValueError("unexpected reply from the translation service") from exc
            detected = detected or found
            parts.append(translated)
    finally:
        if own:
            http.close()
    return detected, " ".join(parts)
