"""
Shared HTTP client configuration for outbound requests to CelesTrak and SATNOGS.

Centralizing this keeps the "please don't get this IP firewalled" concerns —
identifying ourselves, capping concurrency, spacing requests out — consistent
across tle_manager.py and transmitter_manager.py instead of each fetch loop
reinventing (or forgetting) its own choices.

Background: CelesTrak's usage policy documents an IP-level firewall after 50
HTTP errors in a 2-hour window (https://celestrak.org/usage-policy.php).
SATNOGS documents no equivalent limit but has been observed responding with
429 under load. Before 2026-08-11, the per-satellite fallback loops in
tle_manager.py opened a brand-new httpx.AsyncClient (fresh TCP+TLS handshake)
for every single request, ran up to 20 of those at once with zero spacing,
and sent no identifying User-Agent — indistinguishable from an anonymous
burst scanner from the provider's point of view.
"""

from __future__ import annotations

import asyncio
import time
from importlib import metadata

import httpx

try:
    _APP_VERSION = metadata.version("fbsat59")
except metadata.PackageNotFoundError:
    _APP_VERSION = "dev"

# Both CelesTrak's and SATNOGS's usage guidance ask that heavy users identify
# themselves rather than show up as an anonymous "python-httpx/x.y" client.
USER_AGENT = f"FBSAT59/{_APP_VERSION} (+https://github.com/JF9SOM/FBSAT59)"

DEFAULT_HEADERS: dict[str, str] = {"User-Agent": USER_AGENT}

# Concurrency for the per-satellite fallback loops (CelesTrak CATNR queries,
# SATNOGS TLE API lookups). Lowered from 20 (2026-08-11) -- 20 concurrent
# fresh connections landing on the provider at once reads as a burst scan
# regardless of how "polite" the requests themselves are.
PHASE2_CONCURRENCY = 6

# Minimum spacing between the *start* of successive requests in those same
# loops, enforced by RequestPacer below. Concurrency alone doesn't prevent a
# fresh batch of PHASE2_CONCURRENCY workers from all dispatching in the same
# instant every time they finish together; this puts an explicit floor on
# request rate independent of worker count.
PHASE2_MIN_INTERVAL_S = 0.2


def build_async_client(timeout: float | httpx.Timeout) -> httpx.AsyncClient:
    """httpx.AsyncClient identified with our User-Agent and sized for
    PHASE2_CONCURRENCY concurrent callers.

    Callers should open ONE of these per batch (not one per request) and
    share it across all concurrent tasks in that batch -- httpx.AsyncClient
    is safe to use from multiple concurrent coroutines and reuses/pools its
    underlying TCP+TLS connections across calls, which a brand-new client
    per request cannot do.
    """
    limits = httpx.Limits(
        max_connections=PHASE2_CONCURRENCY,
        max_keepalive_connections=PHASE2_CONCURRENCY,
    )
    return httpx.AsyncClient(timeout=timeout, headers=DEFAULT_HEADERS, limits=limits)


class RequestPacer:
    """Enforces a minimum spacing between the start of successive requests,
    shared across however many concurrent workers are dispatching them.

    Without this, reducing concurrency alone still lets a full batch of
    workers dispatch back-to-back every time they happen to finish at the
    same moment. `wait()` blocks the caller until at least `min_interval_s`
    has elapsed since the last dispatch (from any worker), then records the
    new dispatch time -- a simple shared leaky-bucket gate.
    """

    def __init__(self, min_interval_s: float) -> None:
        self._min_interval = min_interval_s
        self._lock = asyncio.Lock()
        self._last_dispatch = 0.0

    async def wait(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_dispatch
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            self._last_dispatch = time.monotonic()
