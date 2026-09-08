"""Tests for comms/aprs/citylookup.py — offline nearest-city lookup, and its
wiring into humanize's Plain view.

Skips gracefully if the bundled GeoNames extract isn't present (e.g. a
minimal install); the humanizer treats "no data" the same as "no city
nearby" and simply omits the suffix.
"""

from __future__ import annotations

import pytest

from comms.aprs.citylookup import nearest_city
from comms.aprs.humanize import humanize_tnc2
from i18n import get_language, set_language

_HAVE_DATA = nearest_city(35.68, 139.69) is not None
pytestmark = pytest.mark.skipif(not _HAVE_DATA, reason="bundled cities15000 data not available")


@pytest.mark.parametrize(
    ("lat", "lon", "country"),
    [
        (35.6895, 139.6917, "JP"),  # Tokyo
        (34.0522, -118.2437, "US"),  # Los Angeles
        (51.5074, -0.1278, "GB"),  # London
        (-33.8688, 151.2093, "AU"),  # Sydney
    ],
)
def test_nearest_city_returns_local_city(lat: float, lon: float, country: str) -> None:
    hit = nearest_city(lat, lon)
    assert hit is not None
    name, cc, dist_km = hit
    assert name
    assert cc == country
    assert dist_km < 25.0  # a major city sits within 25 km of its own coords


def test_nearest_city_none_in_the_open_ocean() -> None:
    # Middle of the South Atlantic — nothing within 50 km.
    assert nearest_city(-30.0, -20.0) is None


def test_nearest_city_respects_max_km() -> None:
    # Tokyo coords, but only accept a city within 100 m.
    assert nearest_city(35.6895, 139.6917, max_km=0.1) is not None
    far = nearest_city(35.9, 139.2, max_km=1.0)  # rural-ish point, tight radius
    assert far is None or far[2] <= 1.0


def test_humanize_position_line_ends_with_nearest_city() -> None:
    prev = get_language()
    set_language("en")
    try:
        out = humanize_tnc2("JA1ZRL>APRS:!3540.00N/13945.00E-hi")
    finally:
        set_language(prev)
    assert out is not None
    assert out.startswith("house · 35.6667°N 139.7500°E · near ")
    assert out.endswith("· “hi”")


def test_humanize_no_city_suffix_far_from_land() -> None:
    prev = get_language()
    set_language("en")
    try:
        out = humanize_tnc2("SHIP>APRS:!3000.00S/02000.00W-at sea")
    finally:
        set_language(prev)
    assert out is not None
    assert " · near " not in out
