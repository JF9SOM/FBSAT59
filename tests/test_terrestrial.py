"""Terrestrial (non-satellite) APRS presets: pseudo-satellite -2, no Doppler, DATA-FM."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from comms.aprs.engine import detect_modem_for_transmitter
from comms.mode_detection import is_aprs_transmitter
from core.engine import TERRESTRIAL_ID, DopplerCalculator, SatelliteEngine
from data.database import SCHEMA_SQL
from data.transmitter_manager import TransmitterManager
from rig.controller import _FT991_MODE_MAP, _SATNOGS_TO_RIGCTLD_MODE, MODE_MAP


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def _terrestrial(db: sqlite3.Connection) -> list[dict]:
    TransmitterManager(db).load_community_transmitters()
    rows = db.execute(
        "SELECT * FROM transmitters WHERE norad_cat_id = ? ORDER BY downlink_low",
        (TERRESTRIAL_ID,),
    ).fetchall()
    return [dict(r) for r in rows]


def test_both_frequencies_are_loaded_as_simplex_data_fm(db: sqlite3.Connection) -> None:
    rows = _terrestrial(db)
    assert [(r["downlink_low"], r["uplink_low"], r["baud"]) for r in rows] == [
        (144_640_000, 144_640_000, 9600),
        (144_660_000, 144_660_000, 1200),
    ]
    assert {r["mode"] for r in rows} == {"FM-D"}
    assert all(r["source"] == "community" and r["alive"] == 1 for r in rows)
    sat = db.execute(
        "SELECT is_hidden FROM satellites WHERE norad_cat_id = ?", (TERRESTRIAL_ID,)
    ).fetchone()
    assert sat["is_hidden"] == 0


def test_aprs_tab_matches_them_and_picks_the_right_baud(db: sqlite3.Connection) -> None:
    for row in _terrestrial(db):
        assert is_aprs_transmitter(row)
        assert detect_modem_for_transmitter(row) == str(row["baud"])


def test_observation_has_zero_range_rate_so_doppler_is_nil() -> None:
    engine = SatelliteEngine(MagicMock(), 35.0, 139.0, 0.0)
    obs = engine.observe(TERRESTRIAL_ID)
    assert obs is not None
    assert obs.range_rate_km_s == 0.0
    corrected, shift = DopplerCalculator.correct_downlink(144_660_000.0, obs.range_rate_km_s)
    assert corrected == 144_660_000.0
    assert shift == 0.0


def test_fm_d_maps_to_data_fm_everywhere() -> None:
    assert _FT991_MODE_MAP["FM-D"] == "A"
    assert _SATNOGS_TO_RIGCTLD_MODE["FM-D"] == "PKTFM"
    assert MODE_MAP["FM-D"] == 4096  # RIG_MODE_PKTFM
