"""Tests for core.autotrack.AutotrackManager.

Covers the cached_elevations contract in particular: a stale entry in that
dict is trusted as-is (Rule 1 never re-queries the engine), which is exactly
what let a satellite's LOS go undetected in main_window.py when the world
map's elevation cache froze because a non-map tab (e.g. METEOR/HRPT) was
active. The fix stopped passing that cache at all, so the live engine is
always consulted -- see the second test below.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from core.autotrack import AutotrackManager

if TYPE_CHECKING:
    from core.engine import PassPredictor, SatelliteEngine

_NORAD = 57166  # METEOR-M N2-3
_XPDR_UUID = "test-xpdr-uuid"


@dataclass
class _FakeObservation:
    elevation_deg: float


class _FakeEngine:
    """Duck-typed stand-in for SatelliteEngine — only observe() is used."""

    def __init__(self, elevations: dict[int, float]) -> None:
        self._elevations = elevations
        self.observe_calls: list[int] = []

    def observe(self, norad_cat_id: int, at: object = None) -> _FakeObservation | None:
        self.observe_calls.append(norad_cat_id)
        if norad_cat_id not in self._elevations:
            return None
        return _FakeObservation(elevation_deg=self._elevations[norad_cat_id])


class _FakePredictor:
    """Duck-typed stand-in for PassPredictor — no upcoming passes."""

    def get_passes(self, norad_cat_id: int, start: object, end: object) -> list[object]:
        return []


def _engine(fake: _FakeEngine) -> SatelliteEngine:
    return cast("SatelliteEngine", fake)


def _predictor(fake: _FakePredictor) -> PassPredictor:
    return cast("PassPredictor", fake)


def _make_conn_with_single_entry() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE autotrack_lists (id INTEGER PRIMARY KEY, name TEXT, sort_order INTEGER)"
    )
    conn.execute(
        """
        CREATE TABLE autotrack_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id INTEGER, norad_cat_id INTEGER, xpdr_uuid TEXT,
            sort_order INTEGER, notes TEXT
        )
        """
    )
    conn.execute("INSERT INTO autotrack_lists (id, name, sort_order) VALUES (1, 'Test', 0)")
    conn.execute(
        "INSERT INTO autotrack_entries (list_id, norad_cat_id, xpdr_uuid, sort_order, notes)"
        " VALUES (1, ?, ?, 0, '')",
        (_NORAD, _XPDR_UUID),
    )
    conn.commit()
    return conn


class TestCachedElevationsFreshness:
    def test_stale_cached_elevations_mask_a_satellite_that_has_set(self) -> None:
        """A caller who opts into cached_elevations gets exactly what it
        asked for: a present key is trusted without ever re-querying the
        engine, even if the satellite has actually set since."""
        conn = _make_conn_with_single_entry()
        mgr = AutotrackManager(conn)
        mgr.set_list(1)
        mgr.mark_searches_ready()
        predictor = _predictor(_FakePredictor())

        # First tick: satellite is up -> commits as the tracked satellite.
        result = mgr.check(_engine(_FakeEngine({_NORAD: 30.0})), predictor)
        assert result == (_NORAD, _XPDR_UUID)
        assert mgr.current_norad == _NORAD

        # Second tick: satellite has actually set, but the supplied cache
        # still claims 30 degrees.
        live_engine = _FakeEngine({_NORAD: -10.0})
        result2 = mgr.check(_engine(live_engine), predictor, cached_elevations={_NORAD: 30.0})

        assert result2 is None  # Rule 1 fires on the stale reading -- no LOS
        assert live_engine.observe_calls == []  # engine never consulted

    def test_no_cache_always_queries_the_engine_live(self) -> None:
        """The fixed call pattern (no cached_elevations argument at all)
        must always ask the engine directly, so a satellite that has
        actually set is detected as such."""
        conn = _make_conn_with_single_entry()
        mgr = AutotrackManager(conn)
        mgr.set_list(1)
        mgr.mark_searches_ready()
        predictor = _predictor(_FakePredictor())

        result = mgr.check(_engine(_FakeEngine({_NORAD: 30.0})), predictor)
        assert result == (_NORAD, _XPDR_UUID)

        live_engine = _FakeEngine({_NORAD: -10.0})
        result2 = mgr.check(_engine(live_engine), predictor)

        assert live_engine.observe_calls == [_NORAD]
        assert result2 is None  # no other/next satellite to switch to
