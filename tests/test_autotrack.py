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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from core.autotrack import AutotrackManager
from core.engine import PassInfo

if TYPE_CHECKING:
    from core.engine import PassPredictor, SatelliteEngine

_NORAD = 57166  # METEOR-M N2-3
_XPDR_UUID = "test-xpdr-uuid"
_NORAD_B = 59051  # METEOR-M N2-4
_XPDR_UUID_B = "test-xpdr-uuid-b"


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


class _FakePredictorWithPasses:
    """Duck-typed PassPredictor stand-in returning scheduled passes per NORAD."""

    def __init__(self, passes_by_norad: dict[int, list[PassInfo]]) -> None:
        self._passes_by_norad = passes_by_norad

    def get_passes(self, norad_cat_id: int, start: object, end: object) -> list[object]:
        return list(self._passes_by_norad.get(norad_cat_id, []))


def _make_pass_info(norad: int, aos: datetime) -> PassInfo:
    return PassInfo(
        norad_cat_id=norad,
        aos=aos,
        tca=aos + timedelta(minutes=5),
        los=aos + timedelta(minutes=10),
        max_elevation_deg=45.0,
        aos_azimuth_deg=90.0,
        los_azimuth_deg=270.0,
        duration_s=600.0,
    )


def _predictor_with_passes(passes_by_norad: dict[int, list[PassInfo]]) -> PassPredictor:
    return cast("PassPredictor", _FakePredictorWithPasses(passes_by_norad))


def _engine(fake: _FakeEngine) -> SatelliteEngine:
    return cast("SatelliteEngine", fake)


def _predictor(fake: _FakePredictor) -> PassPredictor:
    return cast("PassPredictor", fake)


def _make_conn_with_empty_list() -> sqlite3.Connection:
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
    conn.execute("CREATE TABLE satellites (norad_cat_id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO autotrack_lists (id, name, sort_order) VALUES (1, 'Test', 0)")
    conn.commit()
    return conn


def _add_entry(conn: sqlite3.Connection, norad: int = _NORAD, uuid: str = _XPDR_UUID) -> None:
    conn.execute(
        "INSERT INTO autotrack_entries (list_id, norad_cat_id, xpdr_uuid, sort_order, notes)"
        " VALUES (1, ?, ?, 0, '')",
        (norad, uuid),
    )
    conn.commit()


def _make_conn_with_single_entry() -> sqlite3.Connection:
    conn = _make_conn_with_empty_list()
    _add_entry(conn)
    return conn


def _make_conn_with_two_entries() -> sqlite3.Connection:
    conn = _make_conn_with_empty_list()
    _add_entry(conn, norad=_NORAD, uuid=_XPDR_UUID)
    _add_entry(conn, norad=_NORAD_B, uuid=_XPDR_UUID_B)
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


class TestEntriesRefreshFromDb:
    """Adding/removing entries in the currently-selected list must take
    effect on the very next check()/next_satellite_info()/entries() call,
    without needing set_list() to be called again (GitHub Issue #27
    follow-up, 2026-08-23): a user selected a list, then added a satellite
    entry to it -- but AutotrackManager kept using the empty snapshot taken
    at selection time, so Autotrack silently never started tracking.
    """

    def test_entry_added_after_set_list_is_picked_up_by_check(self) -> None:
        conn = _make_conn_with_empty_list()
        mgr = AutotrackManager(conn)
        mgr.set_list(1)
        mgr.mark_searches_ready()
        assert mgr.entries() == []

        _add_entry(conn)

        predictor = _predictor(_FakePredictor())
        result = mgr.check(_engine(_FakeEngine({_NORAD: 30.0})), predictor)

        assert result == (_NORAD, _XPDR_UUID)

    def test_entry_added_after_set_list_is_picked_up_by_entries(self) -> None:
        conn = _make_conn_with_empty_list()
        mgr = AutotrackManager(conn)
        mgr.set_list(1)
        assert mgr.entries() == []

        _add_entry(conn)

        entries = mgr.entries()
        assert len(entries) == 1
        assert entries[0].norad_cat_id == _NORAD

    def test_entry_added_after_set_list_is_picked_up_by_next_satellite_info(self) -> None:
        conn = _make_conn_with_empty_list()
        conn.execute(
            "INSERT INTO satellites (norad_cat_id, name) VALUES (?, 'METEOR-M N2-3')",
            (_NORAD,),
        )
        conn.commit()
        mgr = AutotrackManager(conn)
        mgr.set_list(1)
        mgr.mark_searches_ready()
        assert (
            mgr.next_satellite_info(_engine(_FakeEngine({})), _predictor(_FakePredictor())) is None
        )

        _add_entry(conn)

        info = mgr.next_satellite_info(
            _engine(_FakeEngine({_NORAD: 30.0})), _predictor(_FakePredictor())
        )
        assert info is not None
        name, aos = info
        assert name == "METEOR-M N2-3"

    def test_refreshing_entries_does_not_disturb_tracking_state(self) -> None:
        """_refresh_entries() (called by check() every tick) must not reset
        pass_in_progress/current_norad the way set_list() deliberately
        does -- otherwise Rule 3 (never interrupt a pass in progress) would
        break every single tick."""
        conn = _make_conn_with_single_entry()
        mgr = AutotrackManager(conn)
        mgr.set_list(1)
        mgr.mark_searches_ready()
        predictor = _predictor(_FakePredictor())

        result = mgr.check(_engine(_FakeEngine({_NORAD: 30.0})), predictor)
        assert result == (_NORAD, _XPDR_UUID)
        assert mgr.current_norad == _NORAD

        # Second tick, satellite still up: Rule 1 should keep tracking
        # (return None) rather than re-committing -- confirms _refresh_entries()
        # itself didn't wipe out current_norad/pass_in_progress.
        result2 = mgr.check(_engine(_FakeEngine({_NORAD: 30.0})), predictor)
        assert result2 is None
        assert mgr.current_norad == _NORAD


class TestNextSatelliteInfoExcludesOnlyVisibleCurrent:
    """next_satellite_info() must not exclude `current` from its search
    unless it's genuinely visible right now -- if it's merely check()'s
    Rule 2b earliest-AOS pick that hasn't risen yet, excluding it here
    reports some other, later-rising entry as "Next" instead (GitHub Issue
    #27 follow-up, 2026-08-23): with METEOR M2-3 and M2-4 both in a list
    and M2-4 genuinely the sooner pass, the status label kept showing
    "Next: METEOR M2-3" because M2-4 had already been silently picked as
    `current` by check() and was then wrongly excluded here.
    """

    def test_current_still_shown_as_next_when_not_yet_visible(self) -> None:
        conn = _make_conn_with_two_entries()
        mgr = AutotrackManager(conn)
        mgr.set_list(1)
        mgr.mark_searches_ready()

        now = datetime.now(UTC)
        predictor = _predictor_with_passes(
            {
                _NORAD: [_make_pass_info(_NORAD, now + timedelta(minutes=10))],  # sooner
                _NORAD_B: [_make_pass_info(_NORAD_B, now + timedelta(minutes=60))],
            }
        )
        engine = _engine(_FakeEngine({}))  # neither satellite visible yet

        # First tick: Rule 2b picks _NORAD (the sooner one) as `current`.
        result = mgr.check(engine, predictor)
        assert result == (_NORAD, _XPDR_UUID)
        assert mgr.current_norad == _NORAD

        # Second tick: `current` is still the same earliest pick, so
        # check() returns None (no change) and the status label falls back
        # to next_satellite_info(). It must still report _NORAD, not
        # _NORAD_B, since _NORAD genuinely is the next one to rise.
        result2 = mgr.check(engine, predictor)
        assert result2 is None
        info = mgr.next_satellite_info(engine, predictor)
        assert info is not None
        name, _aos = info
        # No `satellites` row exists in this fixture, so the name falls
        # back to the NORAD id itself.
        assert name == str(_NORAD)

    def test_current_excluded_from_next_when_genuinely_visible(self) -> None:
        """Once `current` is actually above the horizon (Rule 1 keeps
        tracking it), next_satellite_info() must exclude it and report the
        other entry instead."""
        conn = _make_conn_with_two_entries()
        mgr = AutotrackManager(conn)
        mgr.set_list(1)
        mgr.mark_searches_ready()

        now = datetime.now(UTC)
        predictor = _predictor_with_passes(
            {_NORAD_B: [_make_pass_info(_NORAD_B, now + timedelta(minutes=30))]}
        )
        # _NORAD is visible right now.
        engine = _engine(_FakeEngine({_NORAD: 30.0}))

        result = mgr.check(engine, predictor)
        assert result == (_NORAD, _XPDR_UUID)
        assert mgr.current_norad == _NORAD

        # current is visible -> check() keeps tracking it (Rule 1), so the
        # status label falls back to next_satellite_info(), which must
        # exclude _NORAD (already being tracked) and report _NORAD_B.
        result2 = mgr.check(engine, predictor)
        assert result2 is None
        info = mgr.next_satellite_info(engine, predictor)
        assert info is not None
        name, _aos = info
        assert name == str(_NORAD_B)
