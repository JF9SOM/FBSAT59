"""Tests for the dB-calibrated TX Level helpers (FT4 / Q65 tabs)."""

from __future__ import annotations

import pytest

from ui.tx_level import (
    TX_LEVEL_MAX_DB,
    TX_LEVEL_MIN_DB,
    clamp_db,
    db_to_gain,
    format_db,
    load_level_db,
    pct_to_db,
)


def test_db_to_gain_known_values() -> None:
    assert db_to_gain(0) == pytest.approx(1.0)
    assert db_to_gain(-20) == pytest.approx(0.1)
    assert db_to_gain(-6) == pytest.approx(0.501, abs=1e-3)


def test_clamp_db_limits_range() -> None:
    assert clamp_db(5) == TX_LEVEL_MAX_DB
    assert clamp_db(-99) == TX_LEVEL_MIN_DB
    assert clamp_db(-22.6) == -23


def test_pct_to_db_migrates_legacy_percent() -> None:
    assert pct_to_db(100.0) == 0
    assert pct_to_db(7.0) == -23
    assert pct_to_db(3.0) == -30
    assert pct_to_db(0.0) == TX_LEVEL_MIN_DB


def test_load_level_db_prefers_new_key_then_migrates_then_defaults() -> None:
    assert load_level_db({"tx_level_db": -18, "tx_level_pct": 50.0}) == -18
    assert load_level_db({"tx_level_pct": 10.0}) == -20
    assert load_level_db({}) == 0
    assert load_level_db({"tx_level_db": -999}) == TX_LEVEL_MIN_DB


def test_format_db() -> None:
    assert format_db(-23) == "-23 dB"
