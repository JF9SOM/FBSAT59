"""Offline nearest-city lookup for the APRS tab's Plain view.

``humanize`` appends "· near <city>" to a position line. The data is a
bundled GeoNames extract (cities with population >= 15000), so this works
with no network and in a frozen build.

Data file: ``src/data/cities15000.tsv.gz`` — tab-separated
``name<TAB>lat<TAB>lon<TAB>country<TAB>population`` with two leading ``#``
comment lines. Source: https://download.geonames.org/export/dump/ (CC BY 4.0).
"""

from __future__ import annotations

import gzip
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np

# Beyond this, "near <city>" is more misleading than helpful — say nothing.
_DEFAULT_MAX_KM = 50.0
_EARTH_RADIUS_KM = 6371.0088


def _data_path() -> Path:
    name = "cities15000.tsv.gz"
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "data" / name  # type: ignore[attr-defined]
    # src/comms/aprs/citylookup.py -> src/data/<name>
    return Path(__file__).resolve().parents[2] / "data" / name


@lru_cache(maxsize=1)
def _load() -> tuple[list[str], list[str], np.ndarray, np.ndarray] | None:
    """Return (names, countries, lat_rad, lon_rad) or None when unavailable."""
    path = _data_path()
    if not path.is_file():
        return None
    names: list[str] = []
    countries: list[str] = []
    lats: list[float] = []
    lons: list[float] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                if not line or line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 4:
                    continue
                names.append(parts[0])
                lats.append(float(parts[1]))
                lons.append(float(parts[2]))
                countries.append(parts[3])
    except (OSError, ValueError):
        return None
    if not names:
        return None
    return (
        names,
        countries,
        np.radians(np.asarray(lats, dtype=np.float64)),
        np.radians(np.asarray(lons, dtype=np.float64)),
    )


def nearest_city(
    lat_deg: float, lon_deg: float, max_km: float = _DEFAULT_MAX_KM
) -> tuple[str, str, float] | None:
    """Nearest bundled city to (lat, lon).

    Returns (name, country_code, distance_km), or None when the data file is
    missing or the nearest city is farther than *max_km*.
    """
    data = _load()
    if data is None:
        return None
    names, countries, lat_r, lon_r = data
    p_lat = np.radians(lat_deg)
    p_lon = np.radians(lon_deg)
    d_lat = lat_r - p_lat
    d_lon = lon_r - p_lon
    a = np.sin(d_lat / 2) ** 2 + np.cos(p_lat) * np.cos(lat_r) * np.sin(d_lon / 2) ** 2
    dist_km = 2 * _EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))
    i = int(np.argmin(dist_km))
    d = float(dist_km[i])
    if d > max_km:
        return None
    return names[i], countries[i], d
