"""
Favorite group membership (many-to-many between satellites and custom groups).

A satellite can belong to any number of custom favorite groups (Favorite 1, 2, ...).
Membership lives in the ``satellite_favorites`` table. The legacy
``satellites.is_favorite`` / ``satellites.favorite_group`` columns are no longer
read by the app; they are kept in sync (``is_favorite`` = "in at least one group",
``favorite_group`` = lowest group id or 0) purely so an older build opened against
the same database still shows sensible favorites.

None of these helpers commit; callers commit once their whole change is done.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable


def load_all_memberships(conn: sqlite3.Connection) -> dict[int, list[int]]:
    """Return ``{norad: [group_id, ...]}`` for every satellite in at least one group."""
    result: dict[int, list[int]] = {}
    for row in conn.execute(
        "SELECT norad_cat_id, group_id FROM satellite_favorites ORDER BY group_id"
    ).fetchall():
        result.setdefault(int(row[0]), []).append(int(row[1]))
    return result


def get_groups(conn: sqlite3.Connection, norad: int) -> list[int]:
    """Return the sorted group ids a satellite belongs to (empty list if none)."""
    rows = conn.execute(
        "SELECT group_id FROM satellite_favorites WHERE norad_cat_id = ? ORDER BY group_id",
        (norad,),
    ).fetchall()
    return [int(r[0]) for r in rows]


def set_groups(conn: sqlite3.Connection, norad: int, group_ids: Iterable[int]) -> None:
    """Replace a satellite's group membership with ``group_ids`` (empty = remove from all)."""
    wanted = sorted({int(g) for g in group_ids if int(g) > 0})
    conn.execute("DELETE FROM satellite_favorites WHERE norad_cat_id = ?", (norad,))
    conn.executemany(
        "INSERT INTO satellite_favorites (norad_cat_id, group_id) VALUES (?, ?)",
        [(norad, g) for g in wanted],
    )
    _sync_legacy_columns(conn, norad, wanted)


def toggle_group(conn: sqlite3.Connection, norad: int, group_id: int) -> None:
    """Add the satellite to ``group_id`` if it is not in it, otherwise remove it."""
    current = set(get_groups(conn, norad))
    current.symmetric_difference_update({group_id})
    set_groups(conn, norad, current)


def remove_group(conn: sqlite3.Connection, group_id: int) -> None:
    """Drop every membership of a deleted group and resync the legacy columns."""
    affected = [
        int(r[0])
        for r in conn.execute(
            "SELECT norad_cat_id FROM satellite_favorites WHERE group_id = ?", (group_id,)
        ).fetchall()
    ]
    conn.execute("DELETE FROM satellite_favorites WHERE group_id = ?", (group_id,))
    for norad in affected:
        _sync_legacy_columns(conn, norad, get_groups(conn, norad))


def move_groups(conn: sqlite3.Connection, from_norad: int, to_norad: int) -> None:
    """Move ``from_norad``'s memberships onto ``to_norad`` (union), leaving none behind.

    Used when a provisional-ID satellite is linked to its official NORAD ID. Moving
    rather than copying keeps the migration pipeline idempotent: it runs on every
    SATNOGS name sync, so a copy would re-add a group the user later removed from
    the official satellite.
    """
    moved = get_groups(conn, from_norad)
    if not moved:
        return
    set_groups(conn, to_norad, set(get_groups(conn, to_norad)) | set(moved))
    set_groups(conn, from_norad, [])


def _sync_legacy_columns(conn: sqlite3.Connection, norad: int, groups: list[int]) -> None:
    """Keep the legacy ``is_favorite`` / ``favorite_group`` columns consistent."""
    conn.execute(
        "UPDATE satellites SET is_favorite = ?, favorite_group = ? WHERE norad_cat_id = ?",
        (1 if groups else 0, min(groups) if groups else 0, norad),
    )
