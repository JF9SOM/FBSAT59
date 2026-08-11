"""
Shared HTTP client configuration for outbound requests to CelesTrak and SATNOGS.

Centralizing this keeps the "identify ourselves so we don't look like an
anonymous scanner" concern consistent across tle_manager.py and
transmitter_manager.py instead of each fetch call reinventing (or
forgetting) its own choice of headers.

Background: CelesTrak's usage policy documents an IP-level firewall after 50
HTTP errors in a 2-hour window (https://celestrak.org/usage-policy.php).
SATNOGS documents no equivalent limit but has been observed responding with
429 under load. Both usage policies ask heavy users to identify themselves
with a descriptive User-Agent rather than the default "python-httpx/x.y".

2026-08-11 history: this module originally also provided PHASE2_CONCURRENCY /
PHASE2_MIN_INTERVAL_S / RequestPacer / build_async_client() to rate-limit the
per-satellite fallback loops in tle_manager.py's Phase 2a (CelesTrak
individual CATNR) and Phase 2b (SATNOGS individual per-satellite). Later that
same day, Phase 2a/2b were replaced entirely by a single SATNOGS bulk TLE
request (see TLEManager._fetch_satnogs_bulk_tles()), which resolves the same
cases (confirmed by testing: NOAA 18/19, ORIGAMISAT-2, ARICA-2's
satnogs_source_id routing) without any per-satellite looping -- so those
concurrency/pacing helpers no longer have a caller and were removed rather
than kept as unused scaffolding.
"""

from __future__ import annotations

from importlib import metadata

try:
    _APP_VERSION = metadata.version("fbsat59")
except metadata.PackageNotFoundError:
    _APP_VERSION = "dev"

# Both CelesTrak's and SATNOGS's usage guidance ask that heavy users identify
# themselves rather than show up as an anonymous "python-httpx/x.y" client.
USER_AGENT = f"FBSAT59/{_APP_VERSION} (+https://github.com/JF9SOM/FBSAT59)"

DEFAULT_HEADERS: dict[str, str] = {"User-Agent": USER_AGENT}
