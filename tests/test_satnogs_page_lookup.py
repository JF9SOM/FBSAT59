"""SatNOGS page lookup: a satellite listed under another NORAD id opens its named page."""

from __future__ import annotations

import sqlite3
import threading
import types
from typing import Any

import pytest

import ui.main_window as mw

_PAGES = {
    68418: {"sat_id": "OBJECT-C", "name": "Object C"},
    98370: {"sat_id": "OPTISAT-PAGE", "name": "OPTISAT"},
}


class _Resp:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self._results = results

    def raise_for_status(self) -> None:
        return None

    def json(self) -> list[dict[str, Any]]:
        return self._results


class _Client:
    asked: list[int] = []

    def __init__(self, *a: Any, **k: Any) -> None:
        pass

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def get(self, url: str, params: dict[str, Any]) -> _Resp:
        nid = params.get("norad_cat_id")
        if nid is not None:
            _Client.asked.append(int(nid))
            return _Resp([_PAGES[int(nid)]] if int(nid) in _PAGES else [])
        return _Resp([])


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Any:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE satellites (norad_cat_id INTEGER PRIMARY KEY, satnogs_uuid TEXT,"
        " satnogs_source_id INTEGER)"
    )
    conn.execute("INSERT INTO satellites VALUES (68418, 'OBJECT-C', 98370)")  # stale cache
    conn.execute("INSERT INTO satellites VALUES (25544, NULL, NULL)")
    _PAGES[25544] = {"sat_id": "ISS-PAGE", "name": "ISS"}
    opened: list[str] = []
    signal = types.SimpleNamespace(emit=opened.append)
    monkeypatch.setattr(mw.httpx, "Client", _Client)
    _Client.asked = []
    return types.SimpleNamespace(
        _conn=conn,
        _satnogs_open_url=signal,
        _satnogs_network_error=types.SimpleNamespace(emit=lambda e: None),
        _satnogs_not_found=types.SimpleNamespace(emit=lambda: None),
        opened=opened,
        _open_url_app_mode=opened.append,
        _fetch_satnogs_uuid_bg=lambda *a: None,
    )


def test_source_id_page_is_preferred_over_the_bare_object_entry(stub: Any) -> None:
    mw.MainWindow._fetch_satnogs_uuid_bg(stub, 68418, "OPTISAT")
    assert stub.opened == ["https://db.satnogs.org/satellite/OPTISAT-PAGE"]
    assert _Client.asked[0] == 98370


def test_stale_cache_is_ignored_when_a_source_id_exists(
    stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[Any, ...]] = []

    class _T:
        def __init__(self, target: Any, args: tuple[Any, ...], daemon: bool) -> None:
            started.append(args)

        def start(self) -> None:
            return None

    monkeypatch.setattr(threading, "Thread", _T)
    mw.MainWindow._open_in_satnogs(stub, 68418, "OPTISAT")
    assert started == [(68418, "OPTISAT")]  # looked up again, the cache was not used
    assert stub.opened == []


def test_plain_satellite_still_looks_up_by_its_own_norad(stub: Any) -> None:
    mw.MainWindow._fetch_satnogs_uuid_bg(stub, 25544, "ISS")
    assert stub.opened == ["https://db.satnogs.org/satellite/ISS-PAGE"]
