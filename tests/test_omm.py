"""Tests for data/omm.py (CelesTrak OMM -> TLE, Alpha-5 catalogue numbers) and
the TLEManager paths that rely on it (6-digit NORAD objects, 2026-10-09).

No Qt import, so it is safe to run locally.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from skyfield.api import EarthSatellite, load

from data.database import SCHEMA_SQL
from data.omm import (
    alpha5_to_int,
    celestrak_to_tle_text,
    is_provisional_norad,
    omm_json_to_tle_text,
    omm_to_tle,
    parse_tle_norad,
)
from data.tle_manager import TLE_SOURCES, TLEManager

# CelesTrak's 2026-195F record (a 6-digit NORAD id: 100470), as served by FORMAT=JSON.
_OMM_F = {
    "OBJECT_NAME": "OBJECT F",
    "OBJECT_ID": "2026-195F",
    "EPOCH": "2026-10-08T04:33:39.424032",
    "MEAN_MOTION": 15.1009378,
    "ECCENTRICITY": 0.00139736,
    "INCLINATION": 97.5406,
    "RA_OF_ASC_NODE": 354.2969,
    "ARG_OF_PERICENTER": 120.7409,
    "MEAN_ANOMALY": 239.5198,
    "EPHEMERIS_TYPE": 0,
    "CLASSIFICATION_TYPE": "U",
    "NORAD_CAT_ID": 100470,
    "ELEMENT_SET_NO": 999,
    "REV_AT_EPOCH": 665,
    "BSTAR": 0.00023795452,
    "MEAN_MOTION_DOT": 3.821e-05,
    "MEAN_MOTION_DDOT": 0,
}
# The same record under an ordinary 5-digit number.
_OMM_5 = {**_OMM_F, "OBJECT_NAME": "TEST SAT", "OBJECT_ID": "2026-001A", "NORAD_CAT_ID": 68795}


# ---------------------------------------------------------------------------
# Alpha-5 helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("25544", 25544),
        ("00005", 5),
        ("99999", 99999),
        ("A0000", 100000),
        ("A0470", 100470),
        ("H9999", 179999),
        ("J0000", 180000),  # I is skipped
        ("P0000", 230000),  # O is skipped
        ("Z9999", 339999),
    ],
)
def test_alpha5_to_int(field: str, value: int) -> None:
    assert alpha5_to_int(field) == value


@pytest.mark.parametrize("bad", ["I0000", "O0000", "A047", "AB470", ""])
def test_alpha5_rejects_invalid_fields(bad: str) -> None:
    with pytest.raises(ValueError):
        alpha5_to_int(bad)


def test_parse_tle_norad_reads_plain_and_alpha5_line1() -> None:
    assert parse_tle_norad("1 68796U 26088E   26192.83685747  .00002724") == 68796
    assert parse_tle_norad("1 A0470U 26195F   26281.19003963  .00003821") == 100470


def test_provisional_range_stops_at_99999() -> None:
    assert is_provisional_norad(98248)
    assert is_provisional_norad(90000)
    assert not is_provisional_norad(89999)
    assert not is_provisional_norad(100000)
    assert not is_provisional_norad(100470)


# ---------------------------------------------------------------------------
# OMM -> TLE
# ---------------------------------------------------------------------------


def test_omm_to_tle_writes_a_6_digit_number_as_alpha5() -> None:
    name, line1, line2 = omm_to_tle(_OMM_F)
    assert name == "OBJECT F"
    assert line1.startswith("1 A0470U 26195F")
    assert line2.startswith("2 A0470 ")
    assert len(line1) == 69 and len(line2) == 69
    assert parse_tle_norad(line1) == 100470


def test_omm_to_tle_keeps_a_5_digit_number_plain() -> None:
    _name, line1, line2 = omm_to_tle(_OMM_5)
    assert line1.startswith("1 68795U")
    assert line2.startswith("2 68795 ")


def test_the_converted_tle_describes_the_same_orbit() -> None:
    _name, line1, line2 = omm_to_tle(_OMM_F)
    sat = EarthSatellite(line1, line2, "F", load.timescale())
    assert sat.model.satnum == 100470
    assert sat.epoch.utc_datetime().isoformat().startswith("2026-10-08T04:33:39.42")
    assert sat.model.no_kozai * 1440.0 / (2 * 3.141592653589793) == pytest.approx(
        _OMM_F["MEAN_MOTION"], abs=1e-6
    )
    assert float(line2[8:16]) == pytest.approx(_OMM_F["INCLINATION"], abs=1e-4)
    assert float(line2[43:51]) == pytest.approx(_OMM_F["MEAN_ANOMALY"], abs=1e-4)


def test_json_text_becomes_three_line_groups_and_skips_bad_records() -> None:
    text = json.dumps([_OMM_F, {"OBJECT_NAME": "broken"}, _OMM_5])
    out = omm_json_to_tle_text(text).splitlines()
    assert len(out) == 6
    assert out[0] == "OBJECT F" and out[1].startswith("1 A0470U")
    assert out[3] == "TEST SAT" and out[4].startswith("1 68795U")


def test_celestrak_to_tle_text_passes_tle_and_error_text_through() -> None:
    tle = (
        "ISS\n"
        "1 25544U 98067A   26280.5  .0001  00000-0  1-3 0  9990\n"
        "2 25544  51.6 1.0 0001 1 1 15.5 1\n"
    )
    assert celestrak_to_tle_text(tle) == tle
    assert celestrak_to_tle_text("No GP data found") == "No GP data found"
    assert celestrak_to_tle_text("[]") == ""
    assert celestrak_to_tle_text("[not json") == ""


def test_group_sources_are_fetched_as_json() -> None:
    assert all(s["params"]["FORMAT"] == "JSON" for s in TLE_SOURCES)


# ---------------------------------------------------------------------------
# TLEManager
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def _client_returning(body: str) -> tuple[MagicMock, AsyncMock]:
    resp = MagicMock()
    resp.text = body
    resp.raise_for_status = MagicMock()
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    return resp, client


def test_group_fetch_stores_a_6_digit_object_that_the_tle_format_would_drop(
    db: sqlite3.Connection,
) -> None:
    _resp, client = _client_returning(json.dumps([_OMM_F, _OMM_5]))
    mgr = TLEManager(db)
    with patch("data.tle_manager.httpx.AsyncClient") as cls:
        cls.return_value.__aenter__ = AsyncMock(return_value=client)
        cls.return_value.__aexit__ = AsyncMock(return_value=None)
        stats = asyncio.run(mgr.fetch_and_update("celestrak-amateur"))
    assert stats["errors"] == 0
    assert stats["inserted"] == 2
    row = db.execute(
        "SELECT line1, source, tle_group FROM tle_data WHERE norad_cat_id = 100470"
    ).fetchone()
    assert row is not None and row["line1"].startswith("1 A0470U")
    assert row["source"] == "celestrak" and row["tle_group"] == "amateur"
    assert db.execute("SELECT 1 FROM satellites WHERE norad_cat_id = 100470").fetchone()
    # the request asked for JSON
    assert client.get.await_args.kwargs["params"]["FORMAT"] == "JSON"


def test_the_stored_6_digit_tle_is_usable_for_tracking(db: sqlite3.Connection) -> None:
    _resp, client = _client_returning(json.dumps([_OMM_F]))
    mgr = TLEManager(db)
    with patch("data.tle_manager.httpx.AsyncClient") as cls:
        cls.return_value.__aenter__ = AsyncMock(return_value=client)
        cls.return_value.__aexit__ = AsyncMock(return_value=None)
        asyncio.run(mgr.fetch_and_update("celestrak-amateur"))
    sat = mgr.get_earth_satellite(100470)
    assert sat is not None and sat.model.satnum == 100470


def test_group_fetch_with_a_plain_tle_response_still_works(db: sqlite3.Connection) -> None:
    name, line1, line2 = omm_to_tle(_OMM_5)
    _resp, client = _client_returning(f"{name}\n{line1}\n{line2}\n")
    mgr = TLEManager(db)
    with patch("data.tle_manager.httpx.AsyncClient") as cls:
        cls.return_value.__aenter__ = AsyncMock(return_value=client)
        cls.return_value.__aexit__ = AsyncMock(return_value=None)
        stats = asyncio.run(mgr.fetch_and_update("celestrak-amateur"))
    assert stats["inserted"] == 1


def test_single_fetch_accepts_a_6_digit_object(db: sqlite3.Connection) -> None:
    _resp, client = _client_returning(json.dumps([_OMM_F]))
    mgr = TLEManager(db)
    with patch("data.tle_manager.httpx.AsyncClient") as cls:
        cls.return_value.__aenter__ = AsyncMock(return_value=client)
        cls.return_value.__aexit__ = AsyncMock(return_value=None)
        assert asyncio.run(mgr.fetch_single(100470)) is True
    assert db.execute("SELECT 1 FROM tle_data WHERE norad_cat_id = 100470").fetchone()


def test_provisional_fetch_leaves_real_6_digit_satellites_alone(db: sqlite3.Connection) -> None:
    """A real 6-digit satellite must not be treated as a SATNOGS placeholder: it is not in
    SATNOGS's bulk dump, so it used to be run into the "no TLE -> hide" path."""
    for norad, name in ((98248, "JAMX01"), (100470, "OBJECT F")):
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (?, ?, 'alive', 0)",
            (norad, name),
        )
    db.commit()
    mgr = TLEManager(db)
    with patch.object(mgr, "_fetch_satnogs_bulk_tles", AsyncMock(return_value={})):
        stats = asyncio.run(mgr.fetch_provisional_tles())
    assert stats["no_tle"] == 1  # only the real provisional one (98248)
    assert db.execute(
        "SELECT tle_no_result_since FROM satellites WHERE norad_cat_id = 100470"
    ).fetchone()[0] in (None, "")
