"""
AMSAT operational-status パーサー動作確認テスト（operational/partial/non_operational分類）

ネットワーク不要。実際の https://www.amsat.org/status/ のページ構造（2026-09-05 確認:
"Name"列を持つテーブル、各セルのbgcolorが状態を表す。#648fff=Sat/Mode Active、
#ffb000=TLM/Beacon only、C0C0C0/空=無報告）を模した最小限のHTML断片で検証する。
"""

from __future__ import annotations

import sqlite3

import pytest

from data.amsat_status import AMSATStatusFetcher
from data.database import SCHEMA_SQL

_SAMPLE_HTML = """
<html><body>
<table>
<tr><td>Name</td><td>Sep 5</td><td>Sep 4</td></tr>
<tr><td>AO-91_[FM]</td><td bgcolor="#648FFF">1</td><td bgcolor="C0C0C0">1</td></tr>
<tr><td>RS18S_[SSTV]</td><td bgcolor="#FFB000">1</td><td bgcolor="C0C0C0">1</td></tr>
<tr><td>ISS_[FM]</td><td bgcolor="C0C0C0">1</td><td bgcolor="C0C0C0">1</td></tr>
<tr><td>AO-7_[U/v]</td><td bgcolor="#648FFF">1</td><td bgcolor="C0C0C0">1</td></tr>
<tr><td>AO-7_[V/a]</td><td bgcolor="#FFB000">1</td><td bgcolor="C0C0C0">1</td></tr>
</table>
</body></html>
"""


@pytest.fixture
def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


class TestParseTables:
    def test_blue_is_operational(self, db_conn: sqlite3.Connection) -> None:
        fetcher = AMSATStatusFetcher(db_conn)
        result = fetcher._parse_html(_SAMPLE_HTML)
        assert result["ao-91"] == "operational"

    def test_gold_only_is_partial(self, db_conn: sqlite3.Connection) -> None:
        fetcher = AMSATStatusFetcher(db_conn)
        result = fetcher._parse_html(_SAMPLE_HTML)
        assert result["rs18s"] == "partial"

    def test_no_report_is_non_operational(self, db_conn: sqlite3.Connection) -> None:
        fetcher = AMSATStatusFetcher(db_conn)
        result = fetcher._parse_html(_SAMPLE_HTML)
        assert result["iss"] == "non_operational"

    def test_blue_on_one_mode_wins_over_gold_on_another(self, db_conn: sqlite3.Connection) -> None:
        """AO-7 has one active frequency (U/v) and one beacon-only frequency (V/a) --
        the satellite as a whole must be "operational", not "partial"."""
        fetcher = AMSATStatusFetcher(db_conn)
        result = fetcher._parse_html(_SAMPLE_HTML)
        assert result["ao-7"] == "operational"


class TestFetchFollowsRedirects:
    """The old www.amsat.org/status/ address answers 308; the fetch must still work."""

    def test_redirect_is_followed_and_status_saved(
        self, db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        import httpx

        from data import amsat_status

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "www.amsat.org":
                return httpx.Response(308, headers={"location": "https://status.amsat.org/"})
            return httpx.Response(200, text=_SAMPLE_HTML)

        real_client = httpx.AsyncClient

        def fake_client(**kwargs: object) -> httpx.AsyncClient:
            return real_client(transport=httpx.MockTransport(handler), **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(httpx, "AsyncClient", fake_client)
        monkeypatch.setattr(amsat_status, "AMSAT_STATUS_URL", "https://www.amsat.org/status/")
        result = asyncio.run(AMSATStatusFetcher(db_conn).fetch_and_update())
        assert result.get("ao-91") == "operational"
        assert AMSATStatusFetcher(db_conn).load_cached() == result
