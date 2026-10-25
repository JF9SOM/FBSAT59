"""The English UI must never show Japanese; Japanese text exists only where the UI is Japanese.

Telemetry format definitions keep English under the usual key and Japanese under ``<key>_ja``
(see comms.telemetry.decoder._localize); other Japanese text in the source is limited to a short
allow-list. A new Japanese string anywhere else fails here, so it has to be made English (with a
``.po`` translation) or added to the allow-list on purpose.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from comms.telemetry.decoder import _formats_dir, load_format
from i18n import get_language, set_language

_CJK = re.compile(r"[　-ヿ㐀-鿿＀-￯]")
_SRC = Path(__file__).resolve().parent.parent / "src"


def _strings(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Every string in a JSON document, with its path."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _strings(value, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def _format_norads() -> list[int]:
    return sorted(int(p.stem) for p in _formats_dir().glob("*.json"))


@pytest.fixture
def language() -> Iterator[None]:
    previous = get_language()
    yield
    set_language(previous)


@pytest.mark.parametrize("norad", _format_norads())
def test_english_telemetry_formats_have_no_japanese(norad: int, language: None) -> None:
    set_language("en")
    fmt = load_format(norad)
    assert fmt is not None
    found = [(p, s) for p, s in _strings(fmt) if _CJK.search(s)]
    assert not found, f"Japanese text in the English view of {norad}.json: {found[:3]}"
    assert not [p for p, _s in _strings(fmt) if p.rsplit("/", 1)[-1].endswith("_ja")]


@pytest.mark.parametrize("norad", _format_norads())
def test_every_japanese_twin_has_a_different_english_text(norad: int) -> None:
    raw = json.loads((_formats_dir() / f"{norad}.json").read_text(encoding="utf-8"))

    def walk(node: Any) -> Iterator[tuple[Any, Any]]:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.endswith("_ja") and key[:-3] in node:
                    yield node[key[:-3]], value
                yield from walk(value)
        elif isinstance(node, list):
            for item in node:
                yield from walk(item)

    for english, japanese in walk(raw):
        assert english != japanese
        assert not _CJK.search(json.dumps(english, ensure_ascii=False))


def test_the_japanese_ui_uses_the_japanese_texts(language: None) -> None:
    set_language("ja")
    fmt = load_format(62391)
    assert fmt is not None
    dos = fmt["csv_messages"]["DOS"]
    assert dos["label"] == "DOS（ドジメータ/科学ペイロード）"
    assert dos["fields"][0]["label"] == "動作モード"
    assert not [p for p, _s in _strings(fmt) if p.rsplit("/", 1)[-1].endswith("_ja")]
    set_language("en")
    english = load_format(62391)
    assert english is not None
    assert english["csv_messages"]["DOS"]["label"] == "DOS (dosimeter / science payload)"


# Japanese text that may stay in the source: (file under src/, a part of the string).
_ALLOWED_JAPANESE = [
    ("main.py", "FBSAT59 を起動中"),  # startup messages shown only in the Japanese UI
    ("main.py", "地図データ読み込み中"),
    ("main.py", "データベース初期化中"),
    ("main.py", "位置情報取得中"),
    ("main.py", "メイン画面構築中"),
    ("main.py", "FBSAT59 はすでに起動中です"),
    ("ui/main_window.py", "日本語"),  # View > Language: a language is named in itself
    ("ui/telemetry_tab.py", "修復済み・未検証"),  # matches the label of rows logged before 10-22
]


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    ids.add(id(value))
    return ids


def test_no_unlisted_japanese_string_in_the_python_source() -> None:
    unlisted: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = _docstring_ids(tree)
        rel = path.relative_to(_SRC).as_posix()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in docs or not _CJK.search(node.value):
                continue
            if any(rel == f and part in node.value for f, part in _ALLOWED_JAPANESE):
                continue
            unlisted.append(f"{rel}:{node.lineno}: {node.value[:50]!r}")
    assert not unlisted, "Japanese text outside the allow-list:\n" + "\n".join(unlisted)


def test_the_web_pages_scripts_show_no_japanese() -> None:
    """The browser UI is English (index.html is lang=en); comments may be Japanese."""
    shown: list[str] = []
    for path in sorted((_SRC / "web" / "static").glob("*.js")):
        text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
        for number, line in enumerate(text.splitlines(), start=1):
            code = re.sub(r"(?<![:'\"`])//.*$", "", line)
            if _CJK.search(code):
                shown.append(f"{path.name}:{number}: {code.strip()[:60]}")
    assert not shown, "Japanese in a script's code:\n" + "\n".join(shown)


def test_community_transmitter_names_are_english() -> None:
    entries = json.loads((_SRC / "data" / "community_transmitters.json").read_text("utf-8"))
    for entry in entries:
        for key, value in entry.items():
            if isinstance(value, str) and _CJK.search(value):
                assert key.endswith("_ja"), f"{key} of {entry.get('uuid')} has Japanese text"
