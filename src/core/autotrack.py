"""
Autotrack engine — sequential satellite tracking.

AutotrackManager selects the next satellite to track from a user-defined
Autotrack List according to the following priority rules:

1. Current satellite is above min_el → keep tracking (no switch).
2. Current satellite is below min_el:
   a. A satellite is already above min_el → switch immediately.
      Tiebreak: list sort_order (lower = higher priority).
   b. No satellite is currently visible → select the one with the earliest AOS.
      Tiebreak: list sort_order.
3. Overlapping passes: never interrupt a pass in progress.
   Wait for the current satellite's LOS before switching.

Usage::

    mgr = AutotrackManager(conn)
    mgr.set_list(list_id)          # select which Autotrack List to use
    next_norad, xpdr_uuid = mgr.check(engine, min_el)
    if next_norad is not None:
        # switch to next_norad with xpdr_uuid
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.engine import PassPredictor, SatelliteEngine


@dataclass
class AutotrackEntry:
    """One entry in an Autotrack List."""

    entry_id: int
    norad_cat_id: int
    xpdr_uuid: str
    sort_order: int
    notes: str


@dataclass
class AutotrackState:
    """Runtime tracking state."""

    current_norad: int | None = None
    current_xpdr_uuid: str | None = None
    pass_in_progress: bool = False  # True once current sat is above min_el
    passes_searched: bool = False  # True after user triggers pass search


class AutotrackManager:
    """Manages sequential satellite tracking across an Autotrack List.

    Args:
        conn: SQLite connection (read-only usage for list/entry queries).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._list_id: int | None = None
        self._entries: list[AutotrackEntry] = []
        self._state = AutotrackState()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def set_list(self, list_id: int | None) -> None:
        """Select the given Autotrack List id and load its entries.

        Call this when the user selects a list in the Radio Control panel.
        Resets the current tracking state (unlike _refresh_entries(), which
        reloads the same list's entries without disturbing tracking state).
        """
        self._list_id = list_id
        self._state = AutotrackState()
        self._refresh_entries()

    def _refresh_entries(self) -> None:
        """Re-read the currently selected list's entries from the DB.

        Called on every set_list() (a genuine list switch) and also at the
        start of check()/next_satellite_info()/entries() so that adding,
        removing, or reordering entries in the list Autotrack is currently
        using takes effect immediately -- previously this only happened
        the next time set_list() itself was called (e.g. re-selecting the
        list in the combo), so editing entries while a list was already
        selected left AutotrackManager working from a stale snapshot
        (GitHub Issue #27 follow-up, 2026-08-23: adding a satellite to a
        list right after selecting it in the combo left Autotrack showing
        "Run a pass search first" forever, because entries() -- read at
        selection time -- was still empty).
        """
        self._entries = []
        if self._list_id is None:
            return
        rows = self._conn.execute(
            """
            SELECT ae.id, ae.norad_cat_id, ae.xpdr_uuid, ae.sort_order, ae.notes
            FROM autotrack_entries ae
            WHERE ae.list_id = ?
            ORDER BY ae.sort_order ASC, ae.id ASC
            """,
            (self._list_id,),
        ).fetchall()
        self._entries = [
            AutotrackEntry(
                entry_id=int(r["id"]),
                norad_cat_id=int(r["norad_cat_id"]),
                xpdr_uuid=str(r["xpdr_uuid"]),
                sort_order=int(r["sort_order"]),
                notes=str(r["notes"] or ""),
            )
            for r in rows
        ]

    def mark_searches_ready(self) -> None:
        """Call after the user has completed a Group/Pass search.

        Autotrack cannot start until this is called (safety guard so that
        the engine has valid pass data before switching satellites).
        """
        self._state.passes_searched = True

    def reset(self) -> None:
        """Stop autotracking and clear state (e.g. when Autotrack is turned off)."""
        self._state = AutotrackState()

    @property
    def is_ready(self) -> bool:
        """True when a list is loaded and pass search has been performed."""
        return bool(self._entries) and self._state.passes_searched

    @property
    def current_norad(self) -> int | None:
        """The satellite currently being autotracked (None if not started)."""
        return self._state.current_norad

    @property
    def current_xpdr_uuid(self) -> str | None:
        """Transponder UUID of the current autotrack satellite."""
        return self._state.current_xpdr_uuid

    def entries(self) -> list[AutotrackEntry]:
        """Return the current list's entries (ordered by sort_order), freshly
        re-read from the DB -- see _refresh_entries()."""
        self._refresh_entries()
        return list(self._entries)

    def check(
        self,
        engine: SatelliteEngine,
        predictor: PassPredictor,
        min_el: float = 5.0,
        cached_elevations: dict[int, float] | None = None,
    ) -> tuple[int, str] | None:
        """Evaluate whether to switch to a different satellite.

        Called every second from the main timer tick.

        Args:
            engine:            SatelliteEngine for current elevation queries.
            predictor:         PassPredictor for AOS prediction.
            min_el:            Minimum elevation threshold (degrees).
            cached_elevations: Pre-computed {norad: elevation_deg} dict from the
                               world-map update cycle.  When supplied, avoids
                               redundant Skyfield calls for the same tick.

        Returns:
            (norad_cat_id, xpdr_uuid) if a switch should happen, else None.
        """
        self._refresh_entries()
        if not self.is_ready or not self._entries:
            return None

        now = datetime.now(UTC)
        norads = [e.norad_cat_id for e in self._entries]

        # Use cached elevations when available; fall back to live queries
        if cached_elevations is not None:
            elevations = {n: cached_elevations[n] for n in norads if n in cached_elevations}
            missing = [n for n in norads if n not in elevations]
            if missing:
                elevations.update(self._get_elevations(engine, missing))
        else:
            elevations = self._get_elevations(engine, norads)

        current = self._state.current_norad

        # Rule 1: current satellite is above min_el → keep tracking
        if current is not None:
            cur_el = elevations.get(current, -90.0)
            if cur_el >= min_el:
                self._state.pass_in_progress = True
                return None
            # Below min_el but still above the true horizon (0 deg): the
            # pass is still in progress -- either just after AOS (hasn't
            # climbed to min_el yet) or descending toward LOS. Don't hand
            # off to another entry mid-pass (Rule 3), because
            # _get_next_aos() below only considers *future* passes (its
            # `p.aos >= now` filter skips one already under way) -- for the
            # currently-tracked satellite that silently returns its *next*
            # orbit's AOS instead of "now", which can be later than another
            # entry's genuinely upcoming AOS and falsely look like the
            # worse choice, causing a spurious switch away from a satellite
            # that's still overhead (GitHub Issue #27 follow-up,
            # 2026-08-23: two METEOR passes that never overlapped still hit
            # this via the min_el-to-horizon gap, and the resulting rapid
            # LOS/AOS handoff didn't give SatDump's own shutdown time to
            # release the RTL-SDR before the next satellite's start tried
            # to open it, causing an "already claimed" failure).
            if cur_el >= 0.0:
                return None

        # Rule 3: if a pass was in progress and now below min_el → LOS occurred.
        # Allow switch only now (don't interrupt mid-pass).
        # pass_in_progress is reset below when we commit to a switch.

        # Rule 2a: a satellite is already visible → switch immediately
        # (sorted by sort_order via self._entries)
        for entry in self._entries:
            el = elevations.get(entry.norad_cat_id, -90.0)
            if el >= min_el:
                return self._commit_switch(entry)

        # Rule 2b: no satellite visible → find the one with the earliest AOS
        best_entry: AutotrackEntry | None = None
        best_aos: datetime | None = None

        for entry in self._entries:
            aos = self._get_next_aos(predictor, entry.norad_cat_id, now)
            if aos is None:
                continue
            if best_aos is None or aos < best_aos:
                best_aos = aos
                best_entry = entry
            elif aos == best_aos:
                # Tiebreak: list sort_order (entry already first due to ORDER BY)
                if best_entry is None or entry.sort_order < best_entry.sort_order:
                    best_entry = entry

        if best_entry is not None and best_entry.norad_cat_id != current:
            return self._commit_switch(best_entry)

        return None

    def next_satellite_info(
        self,
        engine: SatelliteEngine,
        predictor: PassPredictor,
        min_el: float = 5.0,
    ) -> tuple[str, datetime | None] | None:
        """Return (sat_name, next_aos) for the status label in Radio Control.

        Returns None if no next satellite can be determined.
        """
        self._refresh_entries()
        if not self.is_ready or not self._entries:
            return None

        now = datetime.now(UTC)
        norads = [e.norad_cat_id for e in self._entries]
        elevations = self._get_elevations(engine, norads)

        current = self._state.current_norad
        # Only exclude `current` from the search if it's genuinely being
        # tracked right now (visible above min_el, i.e. Rule 1 kept check()
        # returning None) -- if `current` is merely Rule 2b's
        # earliest-AOS pick that hasn't risen yet (check() also returns
        # None in that case, since the pick didn't change), it *is* the
        # next satellite and excluding it here would report some other,
        # later-rising entry as "Next" instead (GitHub Issue #27 follow-up,
        # 2026-08-23: with METEOR M2-3 and M2-4 both in a list and M2-4
        # genuinely the sooner pass, the status kept showing "Next:
        # METEOR M2-3" because M2-4 had already been silently picked as
        # `current` by check() and was then excluded here).
        current_is_visible = current is not None and elevations.get(current, -90.0) >= min_el

        # Look for the satellite that will come after the current one
        best_entry: AutotrackEntry | None = None
        best_aos: datetime | None = None

        for entry in self._entries:
            if current_is_visible and entry.norad_cat_id == current:
                continue
            el = elevations.get(entry.norad_cat_id, -90.0)
            if el >= min_el:
                best_entry = entry
                best_aos = now
                break
            aos = self._get_next_aos(predictor, entry.norad_cat_id, now)
            if aos is None:
                continue
            if best_aos is None or aos < best_aos:
                best_aos = aos
                best_entry = entry

        if best_entry is None:
            return None

        name_row = self._conn.execute(
            "SELECT name FROM satellites WHERE norad_cat_id = ?",
            (best_entry.norad_cat_id,),
        ).fetchone()
        name = str(name_row["name"]) if name_row else str(best_entry.norad_cat_id)
        return name, best_aos

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _commit_switch(self, entry: AutotrackEntry) -> tuple[int, str]:
        """Update state and return the (norad, xpdr_uuid) switch target."""
        self._state.current_norad = entry.norad_cat_id
        self._state.current_xpdr_uuid = entry.xpdr_uuid
        self._state.pass_in_progress = False
        return entry.norad_cat_id, entry.xpdr_uuid

    @staticmethod
    def _get_elevations(engine: SatelliteEngine, norads: list[int]) -> dict[int, float]:
        """Return {norad: elevation_deg} for each NORAD id."""
        result: dict[int, float] = {}
        for norad in norads:
            obs = engine.observe(norad)
            if obs is not None:
                result[norad] = obs.elevation_deg
        return result

    @staticmethod
    def _get_next_aos(
        predictor: PassPredictor,
        norad: int,
        now: datetime,
    ) -> datetime | None:
        """Return the next AOS for a satellite, or None if unavailable."""
        end = now + timedelta(hours=24)
        passes = predictor.get_passes(norad, start=now, end=end)
        for p in passes:
            if p.aos >= now:
                return p.aos
        return None

    # ------------------------------------------------------------------ #
    # List management helpers (used by Settings dialog)
    # ------------------------------------------------------------------ #

    @staticmethod
    def get_all_lists(conn: sqlite3.Connection) -> list[dict[str, int | str]]:
        """Return all Autotrack Lists ordered by sort_order."""
        rows = conn.execute(
            "SELECT id, name, sort_order FROM autotrack_lists ORDER BY sort_order, id"
        ).fetchall()
        return [
            {"id": int(r["id"]), "name": str(r["name"]), "sort_order": int(r["sort_order"])}
            for r in rows
        ]

    @staticmethod
    def get_entries(conn: sqlite3.Connection, list_id: int) -> list[dict[str, int | str | None]]:
        """Return entries for a list with satellite name and transponder description."""
        rows = conn.execute(
            """
            SELECT ae.id, ae.norad_cat_id, ae.xpdr_uuid, ae.sort_order, ae.notes,
                   s.name AS sat_name,
                   t.description AS xpdr_desc,
                   t.downlink_low, t.uplink_low, t.mode
            FROM autotrack_entries ae
            LEFT JOIN satellites s ON ae.norad_cat_id = s.norad_cat_id
            LEFT JOIN transmitters t ON ae.xpdr_uuid = t.uuid
            WHERE ae.list_id = ?
            ORDER BY ae.sort_order ASC, ae.id ASC
            """,
            (list_id,),
        ).fetchall()
        result: list[dict[str, int | str | None]] = []
        for r in rows:
            result.append(
                {
                    "id": int(r["id"]),
                    "norad_cat_id": int(r["norad_cat_id"]),
                    "xpdr_uuid": str(r["xpdr_uuid"]),
                    "sort_order": int(r["sort_order"]),
                    "notes": str(r["notes"] or ""),
                    "sat_name": str(r["sat_name"]) if r["sat_name"] else None,
                    "xpdr_desc": str(r["xpdr_desc"]) if r["xpdr_desc"] else None,
                    "downlink_low": int(r["downlink_low"]) if r["downlink_low"] else None,
                    "uplink_low": int(r["uplink_low"]) if r["uplink_low"] else None,
                    "mode": str(r["mode"]) if r["mode"] else None,
                }
            )
        return result

    @staticmethod
    def create_list(conn: sqlite3.Connection, name: str) -> int:
        """Create a new Autotrack List. Returns the new list id."""
        max_row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) AS m FROM autotrack_lists"
        ).fetchone()
        new_order = int(max_row["m"]) + 1
        cur = conn.execute(
            "INSERT INTO autotrack_lists (name, sort_order) VALUES (?, ?)",
            (name, new_order),
        )
        conn.commit()
        return int(cur.lastrowid or 0)

    @staticmethod
    def delete_list(conn: sqlite3.Connection, list_id: int) -> None:
        """Delete a list and all its entries (CASCADE)."""
        conn.execute("DELETE FROM autotrack_lists WHERE id = ?", (list_id,))
        conn.commit()

    @staticmethod
    def rename_list(conn: sqlite3.Connection, list_id: int, name: str) -> None:
        """Rename an Autotrack List."""
        conn.execute("UPDATE autotrack_lists SET name = ? WHERE id = ?", (name, list_id))
        conn.commit()

    @staticmethod
    def add_entry(
        conn: sqlite3.Connection,
        list_id: int,
        norad_cat_id: int,
        xpdr_uuid: str,
        notes: str = "",
    ) -> None:
        """Add a satellite+transponder entry to a list."""
        max_row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) AS m FROM autotrack_entries WHERE list_id = ?",
            (list_id,),
        ).fetchone()
        new_order = int(max_row["m"]) + 1
        conn.execute(
            "INSERT INTO autotrack_entries (list_id, norad_cat_id, xpdr_uuid, sort_order, notes)"
            " VALUES (?, ?, ?, ?, ?)",
            (list_id, norad_cat_id, xpdr_uuid, new_order, notes),
        )
        conn.commit()

    @staticmethod
    def remove_entry(conn: sqlite3.Connection, entry_id: int) -> None:
        """Remove an entry from a list."""
        conn.execute("DELETE FROM autotrack_entries WHERE id = ?", (entry_id,))
        conn.commit()

    @staticmethod
    def move_entry_up(conn: sqlite3.Connection, entry_id: int) -> None:
        """Swap this entry's sort_order with the one above it (lower sort_order)."""
        row = conn.execute(
            "SELECT list_id, sort_order FROM autotrack_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            return
        prev = conn.execute(
            "SELECT id, sort_order FROM autotrack_entries"
            " WHERE list_id = ? AND sort_order < ? ORDER BY sort_order DESC LIMIT 1",
            (int(row["list_id"]), int(row["sort_order"])),
        ).fetchone()
        if prev is None:
            return
        conn.execute(
            "UPDATE autotrack_entries SET sort_order = ? WHERE id = ?",
            (int(prev["sort_order"]), entry_id),
        )
        conn.execute(
            "UPDATE autotrack_entries SET sort_order = ? WHERE id = ?",
            (int(row["sort_order"]), int(prev["id"])),
        )
        conn.commit()

    @staticmethod
    def move_entry_down(conn: sqlite3.Connection, entry_id: int) -> None:
        """Swap this entry's sort_order with the one below it (higher sort_order)."""
        row = conn.execute(
            "SELECT list_id, sort_order FROM autotrack_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            return
        nxt = conn.execute(
            "SELECT id, sort_order FROM autotrack_entries"
            " WHERE list_id = ? AND sort_order > ? ORDER BY sort_order ASC LIMIT 1",
            (int(row["list_id"]), int(row["sort_order"])),
        ).fetchone()
        if nxt is None:
            return
        conn.execute(
            "UPDATE autotrack_entries SET sort_order = ? WHERE id = ?",
            (int(nxt["sort_order"]), entry_id),
        )
        conn.execute(
            "UPDATE autotrack_entries SET sort_order = ? WHERE id = ?",
            (int(row["sort_order"]), int(nxt["id"])),
        )
        conn.commit()
