"""
AMSAT Upcoming Satellites ("In Testing" list) パーサー・キャッシュ動作確認テスト

ネットワーク不要。実際の https://www.amsat.org/upcoming-satellites/ のページ構造
（2026-09-05 確認: <p><strong>In Testing:</strong></p> の直後に
Satellite/Uplink/Downlink/Comments 列を持つ <table> が続く）を模した最小限のHTML
断片でパーサーを検証する。
"""

from __future__ import annotations

import sqlite3

import pytest

from data.amsat_upcoming import AMSATUpcomingFetcher
from data.database import SCHEMA_SQL

_SAMPLE_HTML = """
<html><body>
<p class="has-text-color" style="color:#ea0d0d"><strong>In Testing:</strong></p>
<table>
<tbody>
<tr><td>Satellite</td><td>Uplink</td><td>Downlink</td><td>Comments</td></tr>
<tr>
  <td><strong><a href="https://example.org/a">RS83S</a><br/>
      <a href="https://example.org/a">(Lobachevsky)</a></strong></td>
  <td>435.500 MHz</td><td>145.910 MHz</td><td>Telemetry downlink.</td>
</tr>
<tr><td>CANVAS</td><td>437.250 MHz</td><td>437.250 MHz</td><td>Digipeater.</td></tr>
</tbody>
</table>
<p><strong>Upcoming (some of these might never launch):</strong></p>
<table>
<tbody>
<tr><td>Satellite</td><td>Uplink</td><td>Downlink</td><td>Comments</td></tr>
<tr><td>FUTURESAT-1</td><td>145.000 MHz</td><td>435.000 MHz</td><td>Not launched.</td></tr>
</tbody>
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


class TestParseInTestingTable:
    def test_extracts_only_in_testing_rows(self, db_conn: sqlite3.Connection) -> None:
        fetcher = AMSATUpcomingFetcher(db_conn)
        names = fetcher._parse_html(_SAMPLE_HTML)
        assert names == ["RS83S (Lobachevsky)", "CANVAS"]
        # The "Upcoming" table's satellite must not leak into the result.
        assert "FUTURESAT-1" not in names

    def test_no_heading_returns_empty(self, db_conn: sqlite3.Connection) -> None:
        fetcher = AMSATUpcomingFetcher(db_conn)
        assert fetcher._parse_html("<html><body><p>nothing here</p></body></html>") == []


class TestCache:
    def test_save_and_load_cached_round_trip(self, db_conn: sqlite3.Connection) -> None:
        fetcher = AMSATUpcomingFetcher(db_conn)
        assert fetcher.load_cached() is None
        assert fetcher.is_stale() is True

        fetcher._save(["RS83S (Lobachevsky)", "CANVAS"])

        assert fetcher.load_cached() == ["RS83S (Lobachevsky)", "CANVAS"]
        assert fetcher.is_stale() is False
        assert fetcher.is_stale(max_age_hours=0) is True
