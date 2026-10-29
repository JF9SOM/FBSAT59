#!/usr/bin/env python3
"""Make a TLE for a dummy satellite that passes over your QTH a few minutes from now.

For testing the rotator on a high-elevation pass without waiting for a real
one. Add the printed TLE through Satellite > Add Manual TLE (a manual TLE is
never overwritten by automatic updates), select the satellite, connect the
rotator and start tracking. Remove it afterwards with Satellite > Remove
Manual TLE. Do not use the dummy satellite with a radio.

Examples:
    python scripts/make_dummy_pass_tle.py                    # QTH from the app database
    python scripts/make_dummy_pass_tle.py --delay-min 8 --max-el 86
    python scripts/make_dummy_pass_tle.py --lat 35.69 --lon 139.69 --alt 40
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
from skyfield.api import EarthSatellite, load, wgs84

_MU = 398600.4418  # km^3/s^2
_R_EARTH = 6378.137


def _qth_from_database() -> tuple[float, float, float] | None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    try:
        from data.database import get_db_path

        conn = sqlite3.connect(get_db_path())
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'observer_location'"
        ).fetchone()
        conn.close()
        if row is None:
            return None
        d = json.loads(row[0])
        return float(d["latitude_deg"]), float(d["longitude_deg"]), float(d["elevation_m"])
    except Exception:
        return None


def _checksum(line: str) -> int:
    total = 0
    for ch in line[:68]:
        if ch.isdigit():
            total += int(ch)
        elif ch == "-":
            total += 1
    return total % 10


def make_tle(
    norad: int,
    epoch: datetime,
    inclination_deg: float,
    raan_deg: float,
    mean_anomaly_deg: float,
    altitude_km: float,
) -> tuple[str, str]:
    """Circular-orbit TLE lines."""
    a = _R_EARTH + altitude_km
    n_rev_day = math.sqrt(_MU / a**3) * 86400.0 / (2 * math.pi)
    year = epoch.year % 100
    doy = (epoch - datetime(epoch.year, 1, 1, tzinfo=UTC)).total_seconds() / 86400.0 + 1.0
    l1 = f"1 {norad:05d}U 26001A   {year:02d}{doy:012.8f}  .00000000  00000-0  00000-0 0  999"
    l2 = (
        f"2 {norad:05d} {inclination_deg:8.4f} {raan_deg % 360:8.4f} 0000001 "
        f"{0.0:8.4f} {mean_anomaly_deg % 360:8.4f} {n_rev_day:11.8f}    1"
    )
    return l1 + str(_checksum(l1)), l2 + str(_checksum(l2))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--alt", type=float, default=0.0, help="QTH altitude in metres")
    ap.add_argument("--delay-min", type=float, default=10.0, help="minutes until closest approach")
    ap.add_argument("--max-el", type=float, default=85.0, help="wanted maximum elevation (deg)")
    ap.add_argument("--altitude-km", type=float, default=450.0, help="orbit altitude")
    ap.add_argument("--norad", type=int, default=99999)
    ap.add_argument("--name", default="DUMMY-PASS")
    args = ap.parse_args()

    if args.lat is None or args.lon is None:
        qth = _qth_from_database()
        if qth is None:
            sys.exit("QTH not found in the app database: pass --lat and --lon")
        args.lat, args.lon, args.alt = qth
    print(f"QTH: lat {args.lat:.4f} lon {args.lon:.4f} alt {args.alt:.0f} m", file=sys.stderr)

    inc = max(args.lat + 8.0, 51.6)  # must exceed the QTH latitude
    ts = load.timescale()
    tc = datetime.now(UTC) + timedelta(minutes=args.delay_min)
    tc = tc.replace(microsecond=0)
    obs = wgs84.latlon(args.lat, args.lon, elevation_m=args.alt)

    # Analytic start: closest approach at tc with the sub-satellite point on the QTH
    # on the ascending part of the orbit (argument of latitude u, circular orbit).
    u = math.asin(math.sin(math.radians(args.lat)) / math.sin(math.radians(inc)))
    d_lon = math.degrees(math.atan2(math.cos(math.radians(inc)) * math.sin(u), math.cos(u)))
    gmst_deg = float(ts.from_datetime(tc).gmst) * 15.0
    raan0 = args.lon + gmst_deg - d_lon
    m0 = math.degrees(u)

    times = ts.from_datetimes([tc + timedelta(seconds=s) for s in range(-300, 301, 5)])
    best: tuple[float, float, float, tuple[str, str]] | None = None
    for d_raan in np.arange(-4.0, 4.0001, 0.1):
        for d_m in np.arange(-2.0, 2.0001, 0.1):
            l1, l2 = make_tle(args.norad, tc, inc, raan0 + d_raan, m0 + d_m, args.altitude_km)
            sat = EarthSatellite(l1, l2, args.name, ts)
            alt = (sat - obs).at(times).altaz()[0].degrees
            i = int(np.argmax(alt))
            peak_el, peak_t = float(alt[i]), (i - 60) * 5.0
            score = abs(peak_el - args.max_el) + abs(peak_t) / 60.0
            if best is None or score < best[0]:
                best = (score, peak_el, peak_t, (l1, l2))
    assert best is not None
    _score, peak_el, peak_t, (l1, l2) = best
    print(
        f"Closest approach about {tc:%H:%M:%S} UTC ({peak_t:+.0f} s), "
        f"max elevation {peak_el:.1f} deg",
        file=sys.stderr,
    )
    print(args.name)
    print(l1)
    print(l2)


if __name__ == "__main__":
    main()
