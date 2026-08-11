"""Tests for data.http_client (shared User-Agent / concurrency / pacing config
used by tle_manager.py and transmitter_manager.py to avoid being mistaken for
abusive traffic by CelesTrak/SATNOGS).
"""

from __future__ import annotations

import asyncio
import time

from data.http_client import (
    DEFAULT_HEADERS,
    PHASE2_CONCURRENCY,
    PHASE2_MIN_INTERVAL_S,
    USER_AGENT,
    RequestPacer,
    build_async_client,
)


class TestUserAgent:
    def test_identifies_the_app(self) -> None:
        assert "FBSAT59" in USER_AGENT

    def test_default_headers_carries_the_user_agent(self) -> None:
        assert DEFAULT_HEADERS["User-Agent"] == USER_AGENT


class TestBuildAsyncClient:
    def test_client_carries_the_user_agent_header(self) -> None:
        async def _run() -> str | None:
            async with build_async_client(timeout=5.0) as client:
                return client.headers.get("user-agent")

        assert asyncio.run(_run()) == USER_AGENT

    def test_client_connection_pool_matches_phase2_concurrency(self) -> None:
        async def _run() -> tuple[int | None, int | None]:
            async with build_async_client(timeout=5.0) as client:
                limits = client._transport._pool._max_connections  # type: ignore[attr-defined]
                keepalive = client._transport._pool._max_keepalive_connections  # type: ignore[attr-defined]
                return limits, keepalive

        max_conn, max_keepalive = asyncio.run(_run())
        assert max_conn == PHASE2_CONCURRENCY
        assert max_keepalive == PHASE2_CONCURRENCY


class TestRequestPacer:
    def test_first_call_does_not_wait(self) -> None:
        async def _run() -> float:
            pacer = RequestPacer(min_interval_s=1.0)
            start = time.monotonic()
            await pacer.wait()
            return time.monotonic() - start

        assert asyncio.run(_run()) < 0.5

    def test_enforces_minimum_spacing_between_calls(self) -> None:
        interval = 0.05

        async def _run() -> float:
            pacer = RequestPacer(min_interval_s=interval)
            await pacer.wait()
            start = time.monotonic()
            await pacer.wait()
            return time.monotonic() - start

        elapsed = asyncio.run(_run())
        assert elapsed >= interval * 0.8  # allow scheduling jitter

    def test_concurrent_callers_are_serialized_not_overlapped(self) -> None:
        """Several coroutines calling wait() "simultaneously" must still come
        out spaced at least min_interval apart -- this is what lets a batch
        of PHASE2_CONCURRENCY concurrent workers still respect one shared
        request-rate floor instead of all dispatching in the same instant.
        """
        interval = 0.02
        n = 5

        async def _run() -> list[float]:
            pacer = RequestPacer(min_interval_s=interval)
            timestamps: list[float] = []

            async def _worker() -> None:
                await pacer.wait()
                timestamps.append(time.monotonic())

            await asyncio.gather(*[_worker() for _ in range(n)])
            return sorted(timestamps)

        timestamps = asyncio.run(_run())
        gaps = [b - a for a, b in zip(timestamps, timestamps[1:], strict=False)]
        assert all(gap >= interval * 0.8 for gap in gaps)

    def test_zero_interval_never_waits(self) -> None:
        async def _run() -> float:
            pacer = RequestPacer(min_interval_s=0.0)
            await pacer.wait()
            start = time.monotonic()
            await pacer.wait()
            return time.monotonic() - start

        assert asyncio.run(_run()) < 0.1


def test_phase2_concurrency_is_lower_than_the_old_hardcoded_20() -> None:
    """Regression guard for the 2026-08-11 change: 20 concurrent fresh
    connections with no pacing was indistinguishable from a burst scan.
    """
    assert PHASE2_CONCURRENCY < 20
    assert PHASE2_MIN_INTERVAL_S > 0
