"""Tests for data.http_client (shared User-Agent used by tle_manager.py and
transmitter_manager.py to avoid being mistaken for anonymous scanner traffic
by CelesTrak/SATNOGS).
"""

from __future__ import annotations

from data.http_client import DEFAULT_HEADERS, USER_AGENT


class TestUserAgent:
    def test_identifies_the_app(self) -> None:
        assert "FBSAT59" in USER_AGENT

    def test_default_headers_carries_the_user_agent(self) -> None:
        assert DEFAULT_HEADERS["User-Agent"] == USER_AGENT
