"""
Transponder management module

Manages data retrieved from SATNOGS and manually added data in an integrated way.
Records with manual_override=True are not overwritten by SATNOGS sync.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SATNOGS_API_BASE = "https://db.satnogs.org/api"
SATNOGS_TRANSMITTERS_URL = f"{SATNOGS_API_BASE}/transmitters/"
SATNOGS_SATELLITES_URL = f"{SATNOGS_API_BASE}/satellites/"

# Path to the bundled community transmitter frequency database.
# In a PyInstaller bundle the file is collected to data/ under _MEIPASS.
_COMMUNITY_JSON = (
    Path(sys._MEIPASS) / "data" / "community_transmitters.json"  # type: ignore[attr-defined]
    if getattr(sys, "frozen", False)
    else Path(__file__).parent / "community_transmitters.json"
)

# Path to the bundled EME frequency database (Moon-only, not stored in SQLite).
_EME_JSON = (
    Path(sys._MEIPASS) / "data" / "eme_frequencies.json"  # type: ignore[attr-defined]
    if getattr(sys, "frozen", False)
    else Path(__file__).parent / "eme_frequencies.json"
)

# Sentinel NORAD ID used for the Moon throughout the UI layer.
_MOON_ID: int = -1

# Matches "CTCSS 67.0 Hz" or "CTCSS 100 Hz" in transmitter description text
_CTCSS_RE = re.compile(r"CTCSS\s+([\d.]+)\s*Hz", re.IGNORECASE)

# Matches the numeric placeholder names this module falls back to when no real
# satellite name is known yet ("#12345", "Satellite #12345"). Space-Track's own
# "OBJECT X" placeholder is checked separately since it never carries a "#".
_PLACEHOLDER_NAME_RE = re.compile(r"^(Satellite\s+)?#\d+$", re.IGNORECASE)


def _is_placeholder_name(name: str) -> bool:
    """True when *name* is one of our own numeric placeholders or a
    Space-Track generic object name — i.e. safe to overwrite with a real
    name once one becomes known."""
    return name.upper().startswith("OBJECT ") or bool(_PLACEHOLDER_NAME_RE.match(name))


# SatNOGS status → DB status normalization map
# 'future'/'re-entered' are converted to match the CHECK constraint ('alive','dead','unknown')
_SATNOGS_STATUS_MAP: dict[str, str] = {
    "alive": "alive",
    "dead": "dead",
    "re-entered": "dead",
    "future": "unknown",
}


class TransmitterManager:
    """
    Class managing transponder CRUD operations and SATNOGS synchronization.
    Called from both the UI thread and background threads;
    each method uses an independent DB connection (thread-safe).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #

    def get_transmitters(
        self,
        norad_cat_id: int,
        include_dead: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Return the transponder list for the specified satellite.
        Returns manually added data and SATNOGS data in an integrated way.
        For MOON_ID (-1) returns the static EME frequency list from eme_frequencies.json.
        """
        if norad_cat_id == _MOON_ID:
            return self._get_eme_transmitters()

        query = """
            SELECT * FROM transmitters
            WHERE norad_cat_id = ?
            {}
            ORDER BY alive DESC, source DESC, description
        """.format("" if include_dead else "AND alive = 1")

        rows = self._conn.execute(query, (norad_cat_id,)).fetchall()
        return [dict(r) for r in rows]

    def _get_eme_transmitters(self) -> list[dict[str, Any]]:
        """Load and return the static EME frequency table from eme_frequencies.json."""
        try:
            with _EME_JSON.open(encoding="utf-8") as fh:
                entries: list[dict[str, Any]] = json.load(fh)
            return [e for e in entries if "uuid" in e]
        except Exception:
            return []

    def get_all_satellites(self) -> list[dict[str, Any]]:
        """Return the list of trackable satellites (those with TLE data)"""
        rows = self._conn.execute("""
            SELECT s.*, t.quality_score, t.fetched_at as tle_fetched_at
            FROM satellites s
            LEFT JOIN tle_data t ON s.norad_cat_id = t.norad_cat_id
            ORDER BY s.name
        """).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Community transmitter loader
    # ------------------------------------------------------------------ #

    def load_community_transmitters(self) -> dict[str, int]:
        """
        Load bundled community transmitters from community_transmitters.json
        into the DB (source='community').

        - Skips comment-only entries (no 'uuid' key).
        - Does NOT overwrite records where source='manual'.
        - On conflict (same uuid), updates all fields unless manual_override=1.
        - Returns: {"inserted": N, "updated": N, "skipped": N}
        """
        if not _COMMUNITY_JSON.exists():
            return {"inserted": 0, "updated": 0, "skipped": 0}

        with open(_COMMUNITY_JSON, encoding="utf-8") as fh:
            entries: list[dict[str, Any]] = json.load(fh)

        stats = {"inserted": 0, "updated": 0, "skipped": 0}
        now = datetime.now(UTC).isoformat()

        for entry in entries:
            if "uuid" not in entry:
                continue  # skip comment-only objects

            xpdr_uuid: str = entry["uuid"]
            norad_cat_id: int = entry["norad_cat_id"]

            existing = self._conn.execute(
                "SELECT source, manual_override FROM transmitters WHERE uuid = ?",
                (xpdr_uuid,),
            ).fetchone()

            if existing:
                if existing["source"] == "manual" or existing["manual_override"]:
                    stats["skipped"] += 1
                    continue
                self._conn.execute(
                    """
                    UPDATE transmitters SET
                        norad_cat_id=?, description=?, type=?,
                        uplink_low=?, uplink_high=?, downlink_low=?, downlink_high=?,
                        mode=?, invert=?, baud=?,
                        ctcss_tone=?, ctcss_tone_type=?,
                        alive=1, source='community', updated_at=?
                    WHERE uuid=?
                    """,
                    (
                        norad_cat_id,
                        entry.get("description", ""),
                        entry.get("type", "Transponder"),
                        entry.get("uplink_low"),
                        entry.get("uplink_high"),
                        entry.get("downlink_low"),
                        entry.get("downlink_high"),
                        entry.get("mode"),
                        int(bool(entry.get("invert", False))),
                        entry.get("baud"),
                        entry.get("ctcss_tone"),
                        None,
                        now,
                        xpdr_uuid,
                    ),
                )
                stats["updated"] += 1
            else:
                # Ensure the satellite record exists
                self._conn.execute(
                    "INSERT OR IGNORE INTO satellites (norad_cat_id, name, updated_at)"
                    " VALUES (?, ?, ?)",
                    (norad_cat_id, entry.get("satellite_name", f"#{norad_cat_id}"), now),
                )
                self._conn.execute(
                    """
                    INSERT INTO transmitters (
                        uuid, norad_cat_id, description, type,
                        uplink_low, uplink_high, downlink_low, downlink_high,
                        mode, invert, baud,
                        ctcss_tone, ctcss_tone_type,
                        alive, source, manual_override, notes, updated_at
                    ) VALUES (
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?,
                        1, 'community', 0, ?, ?
                    )
                    """,
                    (
                        xpdr_uuid,
                        norad_cat_id,
                        entry.get("description", ""),
                        entry.get("type", "Transponder"),
                        entry.get("uplink_low"),
                        entry.get("uplink_high"),
                        entry.get("downlink_low"),
                        entry.get("downlink_high"),
                        entry.get("mode"),
                        int(bool(entry.get("invert", False))),
                        entry.get("baud"),
                        entry.get("ctcss_tone"),
                        None,
                        entry.get("notes", ""),
                        now,
                    ),
                )
                stats["inserted"] += 1

        self._conn.commit()
        return stats

    # ------------------------------------------------------------------ #
    # Manual add / edit / delete
    # ------------------------------------------------------------------ #

    def add_manual_transmitter(
        self,
        norad_cat_id: int,
        description: str,
        downlink_low: int,
        mode: str,
        *,
        uplink_low: int | None = None,
        uplink_high: int | None = None,
        downlink_high: int | None = None,
        invert: bool = False,
        ctcss_tone: float | None = None,
        ctcss_tone_type: str | None = None,
        baud: int | None = None,
        notes: str = "",
        xpdr_type: str = "Transponder",
        manual_override: bool = True,
    ) -> str:
        """
        Manually add a transponder.
        Returns: the generated UUID
        """
        new_uuid = f"manual-{uuid.uuid4()}"
        now = datetime.now(UTC).isoformat()

        # Register a placeholder satellite record if it does not exist
        self._conn.execute(
            """
            INSERT OR IGNORE INTO satellites (norad_cat_id, name, updated_at)
            VALUES (?, ?, ?)
        """,
            (norad_cat_id, f"Satellite #{norad_cat_id}", now),
        )

        self._conn.execute(
            """
            INSERT INTO transmitters (
                uuid, norad_cat_id, description, type,
                uplink_low, uplink_high, downlink_low, downlink_high,
                mode, invert, baud,
                ctcss_tone, ctcss_tone_type,
                alive, source, manual_override, notes, updated_at
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                1, 'manual', ?, ?, ?
            )
        """,
            (
                new_uuid,
                norad_cat_id,
                description,
                xpdr_type,
                uplink_low,
                uplink_high,
                downlink_low,
                downlink_high,
                mode,
                int(invert),
                baud,
                ctcss_tone,
                ctcss_tone_type,
                int(manual_override),
                notes,
                now,
            ),
        )
        self._conn.commit()
        return new_uuid

    def update_transmitter(
        self,
        xpdr_uuid: str,
        **fields: Any,
    ) -> None:
        """
        Update a transponder.
        If manual_override is explicitly passed, that value is used;
        otherwise the current value is preserved.
        """
        allowed = {
            "description",
            "type",
            "uplink_low",
            "uplink_high",
            "downlink_low",
            "downlink_high",
            "mode",
            "invert",
            "ctcss_tone",
            "ctcss_tone_type",
            "baud",
            "alive",
            "notes",
            "manual_override",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return

        updates["updated_at"] = datetime.now(UTC).isoformat()

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [xpdr_uuid]
        self._conn.execute(
            f"UPDATE transmitters SET {set_clause} WHERE uuid = ?",
            values,
        )
        self._conn.commit()

    def delete_transmitter(self, xpdr_uuid: str) -> None:
        """Delete a transponder (recommended only for manually added ones)"""
        self._conn.execute("DELETE FROM transmitters WHERE uuid = ?", (xpdr_uuid,))
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Provisional-ID migration pipeline
    # ------------------------------------------------------------------ #

    def _run_migration_pipeline(self, fake_id: int, real_id: int) -> None:
        """Migrate a satellite from a provisional NORAD ID to the official NORAD ID.

        Called when the official NORAD ID is confirmed (either via norad_follow_id in
        the SATNOGS satellite API, or when the SATNOGS TLE API returns a TLE whose
        line1 encodes a different NORAD than the provisional ID).

        Idempotent: safe to call multiple times on the same satellite pair.

        Refuses to run entirely if the official satellite already has its own
        transmitters (see guard below) — a well-established satellite must
        never be linked to an unrelated provisional ID.

        Steps executed in order:
          1. Ensure the official satellite record exists in the DB.
          2. Update the official satellite's name if it is a Space-Track placeholder.
          3. Migrate the TLE from provisional → official (skipped when a manual TLE
             already exists on the official side).
          4. Migrate transmitters from provisional → official (skipped when the
             official side already has transmitters).
          5. Copy is_favorite from provisional → official.
          6. Record satnogs_source_id = fake_id on the official satellite so that
             future SATNOGS syncs query under the provisional ID.
          7. Hide the provisional satellite (is_hidden = 2).
        """
        now = datetime.now(UTC).isoformat()

        # --- Step 1: load provisional satellite info ---------------------------
        fake_row = self._conn.execute(
            "SELECT name, status, alt_names, is_favorite FROM satellites WHERE norad_cat_id = ?",
            (fake_id,),
        ).fetchone()
        if not fake_row:
            return  # Nothing to migrate

        # Guard: an official satellite that already has its own established
        # transmitters is not a placeholder waiting for an identity. Refuse to
        # link it to an unrelated provisional ID — this happens for real when a
        # freshly deployed CubeSat's TLE briefly matches its host satellite's
        # orbit right after deployment (e.g. Coconut/98292 vs. ISS/25544 on
        # 2026-07-02), which would otherwise hijack the host's satnogs_source_id.
        real_tx_count = self._conn.execute(
            "SELECT COUNT(*) FROM transmitters WHERE norad_cat_id = ?",
            (real_id,),
        ).fetchone()[0]
        if real_tx_count > 0:
            logger.warning(
                "Skipping migration pipeline %d -> %d: official satellite "
                "already has %d transmitter(s) of its own",
                fake_id,
                real_id,
                real_tx_count,
            )
            return

        # Ensure the official satellite record exists
        self._conn.execute(
            "INSERT OR IGNORE INTO satellites (norad_cat_id, name, status, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (real_id, fake_row["name"], fake_row["status"] or "unknown", now),
        )

        # --- Step 2: update placeholder name on the official satellite ----------
        real_row = self._conn.execute(
            "SELECT name FROM satellites WHERE norad_cat_id = ?",
            (real_id,),
        ).fetchone()
        if real_row:
            real_name = str(real_row["name"])
            # Replace Space-Track generic object names or internal placeholder names
            if _is_placeholder_name(real_name):
                self._conn.execute(
                    "UPDATE satellites SET name = ?, updated_at = ? WHERE norad_cat_id = ?",
                    (fake_row["name"], now, real_id),
                )

        # --- Step 3: TLE migration ---------------------------------------------
        real_tle = self._conn.execute(
            "SELECT source FROM tle_data WHERE norad_cat_id = ?",
            (real_id,),
        ).fetchone()
        if real_tle is None:
            # No TLE on official side yet; try to copy from provisional side
            fake_tle = self._conn.execute(
                "SELECT * FROM tle_data WHERE norad_cat_id = ?",
                (fake_id,),
            ).fetchone()
            if fake_tle and fake_tle["source"] != "manual":
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO tle_data
                        (norad_cat_id, name, line1, line2, epoch,
                         source, tle_group, fetched_at, quality_score)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        real_id,
                        fake_tle["name"],
                        fake_tle["line1"],
                        fake_tle["line2"],
                        fake_tle["epoch"],
                        fake_tle["source"],
                        fake_tle["tle_group"],
                        fake_tle["fetched_at"],
                        fake_tle["quality_score"],
                    ),
                )
        # If real_tle already exists (especially manual) → leave it untouched.

        # --- Step 4: transmitter migration -------------------------------------
        real_tx_count = self._conn.execute(
            "SELECT COUNT(*) FROM transmitters WHERE norad_cat_id = ?",
            (real_id,),
        ).fetchone()[0]
        if real_tx_count == 0:
            # Migrate all provisional transmitters to the official ID
            self._conn.execute(
                "UPDATE transmitters SET norad_cat_id = ? WHERE norad_cat_id = ?",
                (real_id, fake_id),
            )
        # If transmitters are already on the official side → leave them alone.

        # --- Step 5: copy is_favorite -----------------------------------------
        if fake_row["is_favorite"]:
            self._conn.execute(
                "UPDATE satellites SET is_favorite = 1 WHERE norad_cat_id = ?",
                (real_id,),
            )

        # --- Step 6: record satnogs_source_id on the official satellite --------
        self._conn.execute(
            "UPDATE satellites SET satnogs_source_id = ?, updated_at = ? WHERE norad_cat_id = ?",
            (fake_id, now, real_id),
        )

        # --- Step 7: hide the provisional satellite ----------------------------
        self._conn.execute(
            "UPDATE satellites SET is_hidden = 2, updated_at = ? WHERE norad_cat_id = ?",
            (now, fake_id),
        )

        self._conn.commit()

    # ------------------------------------------------------------------ #
    # SATNOGS sync
    # ------------------------------------------------------------------ #

    async def sync_from_satnogs(
        self,
        norad_cat_id: int | None = None,
        progress_callback: Any = None,
        target_norad_cat_id: int | None = None,
    ) -> dict[str, int]:
        """
        Fetch transponder information from SATNOGS and update the DB.
        Records with manual_override=True are not overwritten.

        Args:
            norad_cat_id:        If specified, syncs only that satellite. None syncs all.
            progress_callback:   Callback receiving (current, total)
            target_norad_cat_id: Override for the storage NORAD ID.
                                 Used when CelesTrak and SatNOGS have different NORADs.
                                 When specified, all fetched records are saved under this NORAD.

        Returns:
            {"inserted": N, "updated": N, "skipped": N}
        """
        params: dict[str, Any] = {"format": "json", "status": "active"}
        if norad_cat_id:
            params["satellite__norad_cat_id"] = norad_cat_id

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(SATNOGS_TRANSMITTERS_URL, params=params)
            r.raise_for_status()
            transmitters: list[dict[str, Any]] = r.json()

        stats = {"inserted": 0, "updated": 0, "skipped": 0}
        now = datetime.now(UTC).isoformat()

        for i, xpdr in enumerate(transmitters):
            if progress_callback:
                progress_callback(i + 1, len(transmitters))

            xpdr_uuid = xpdr.get("uuid", "")
            if not xpdr_uuid:
                continue

            # Skip records with manual_override=1
            existing = self._conn.execute(
                "SELECT manual_override, source FROM transmitters WHERE uuid = ?",
                (xpdr_uuid,),
            ).fetchone()

            if existing and (
                existing["manual_override"] or existing["source"] in ("manual", "community")
            ):
                stats["skipped"] += 1
                continue

            sat_id = xpdr.get("norad_cat_id") or xpdr.get("satellite__norad_cat_id")
            if not sat_id:
                continue

            # If norad_follow_id (official NORAD) exists, use it as the storage destination.
            # This automatically links provisional NORAD (e.g. 98325) data
            # to the official NORAD (e.g. 68795).
            auto_storage = (
                xpdr.get("norad_follow_id") or xpdr.get("satellite__norad_cat_id") or sat_id
            )

            # satnogs_source_id routing: if any satellite in our DB has registered this
            # SATNOGS provisional ID as its transmitter query key, route to that satellite.
            # This handles post-migration satellites where SATNOGS still stores transmitters
            # under the provisional ID even after norad_follow_id was not set in the API.
            if target_norad_cat_id is None:
                source_sat = self._conn.execute(
                    "SELECT norad_cat_id FROM satellites WHERE satnogs_source_id = ?",
                    (int(sat_id),),
                ).fetchone()
                if source_sat:
                    auto_storage = source_sat["norad_cat_id"]
                    # Ensure the provisional satellite stays hidden
                    self._conn.execute(
                        "UPDATE satellites SET is_hidden = 2"
                        " WHERE norad_cat_id = ? AND is_hidden = 0",
                        (int(sat_id),),
                    )

            # Prefer target_norad_cat_id when explicitly specified (backward compatibility)
            storage_id = target_norad_cat_id if target_norad_cat_id is not None else auto_storage

            # Ensure the satellite record exists. Use a numeric placeholder, never
            # the transmitter's own description — sync_satellite_names() only
            # recognizes "#NNNNN"/"OBJECT ..." as placeholders worth overwriting
            # once the real name is known, so a description here would stick
            # forever (e.g. "Mode U - CW" as a permanent "satellite name").
            self._conn.execute(
                """
                INSERT OR IGNORE INTO satellites (norad_cat_id, name, updated_at)
                VALUES (?, ?, ?)
            """,
                (storage_id, f"#{storage_id}", now),
            )

            # Provisional NORAD differs from official NORAD → auto-hide the
            # provisional-NORAD satellite (is_hidden=2).
            # Not applicable when target_norad_cat_id is externally specified
            # (backward compatibility).
            if target_norad_cat_id is None and int(auto_storage) != int(sat_id):
                self._conn.execute(
                    "UPDATE satellites SET is_hidden = 2 WHERE norad_cat_id = ? AND is_hidden = 0",
                    (int(sat_id),),
                )

            # Use CTCSS from API when available; otherwise extract from description text.
            api_ctcss = xpdr.get("ctcss_tone")
            if api_ctcss is None:
                m = _CTCSS_RE.search(xpdr.get("description", ""))
                api_ctcss = float(m.group(1)) if m else None

            row = (
                xpdr_uuid,
                storage_id,
                xpdr.get("description", ""),
                xpdr.get("type") or "Transponder",
                xpdr.get("uplink_low"),
                xpdr.get("uplink_high"),
                xpdr.get("downlink_low"),
                xpdr.get("downlink_high"),
                xpdr.get("mode"),
                int(bool(xpdr.get("invert", False))),
                xpdr.get("baud"),
                api_ctcss,
                None,  # tone_type: not available in SATNOGS
                int(bool(xpdr.get("alive", True))),
                now,
            )

            if existing:
                self._conn.execute(
                    """
                    UPDATE transmitters SET
                        description=?, type=?,
                        uplink_low=?, uplink_high=?,
                        downlink_low=?, downlink_high=?,
                        mode=?, invert=?, baud=?,
                        ctcss_tone=?, ctcss_tone_type=?, alive=?, updated_at=?
                    WHERE uuid=?
                """,
                    row[2:] + (xpdr_uuid,),
                )
                stats["updated"] += 1
            else:
                self._conn.execute(
                    """
                    INSERT INTO transmitters (
                        uuid, norad_cat_id, description, type,
                        uplink_low, uplink_high, downlink_low, downlink_high,
                        mode, invert, baud,
                        ctcss_tone, ctcss_tone_type,
                        alive, source, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'satnogs', ?)
                """,
                    row,
                )
                stats["inserted"] += 1

        # Backfill ctcss_tone for any transmitter whose description contains CTCSS data
        # but whose ctcss_tone column is still NULL (covers pre-existing records).
        for brow in self._conn.execute(
            "SELECT uuid, description FROM transmitters"
            " WHERE ctcss_tone IS NULL AND description LIKE '%CTCSS%'"
        ).fetchall():
            m = _CTCSS_RE.search(str(brow["description"]))
            if m:
                self._conn.execute(
                    "UPDATE transmitters SET ctcss_tone = ? WHERE uuid = ?",
                    (float(m.group(1)), brow["uuid"]),
                )

        # Auto-hide orphan satellites with 0 transmitters and not registered
        # in SatNOGS (status='unknown').
        # This is determined from the final state after sync_from_satnogs() completes,
        # so it runs only once after all transmitters have been processed.
        self._conn.execute(
            """
            UPDATE satellites SET is_hidden = 2
            WHERE norad_cat_id NOT IN (SELECT DISTINCT norad_cat_id FROM transmitters)
            AND status = 'unknown'
            AND is_hidden = 0
            """
        )

        self._conn.commit()
        self._log_sync("satnogs", stats)
        return stats

    async def sync_satellite_names(
        self,
        progress_callback: Any = None,
    ) -> dict[str, int]:
        """Fetch all satellite names from SATNOGS and update the satellites table.

        CelesTrak sometimes uses provisional names (e.g. OBJECT C), so the official name
        is fetched from SATNOGS and used to overwrite it.
        Satellites not in the DB (TLE not yet fetched) are skipped.

        Returns:
            {"updated": N, "skipped": N}
        """
        stats = {"updated": 0, "skipped": 0}
        now = datetime.now(UTC).isoformat()
        params: dict[str, Any] = {"format": "json"}
        next_url: str | None = SATNOGS_SATELLITES_URL
        total_processed = 0

        async with httpx.AsyncClient(timeout=60.0) as client:
            while next_url:
                r = await client.get(next_url, params=params)
                r.raise_for_status()
                data = r.json()
                params = {}

                if isinstance(data, dict):
                    satellites: list[Any] = list(data.get("results", []))
                    next_raw = data.get("next")
                    next_url = str(next_raw) if next_raw else None
                else:
                    satellites = list(data)
                    next_url = None

                for sat in satellites:
                    norad_raw = sat.get("norad_cat_id")
                    name = str(sat.get("name", "")).strip()
                    if not norad_raw or not name:
                        stats["skipped"] += 1
                        continue

                    norad = int(norad_raw)
                    raw_status = str(sat.get("status", "unknown")).lower()
                    status = _SATNOGS_STATUS_MAP.get(raw_status, "unknown")

                    # Parse alias names (e.g. "ORARI, IO-86, YB0X") into a JSON list
                    names_raw = str(sat.get("names", "") or "").strip()
                    alt_names_json = json.dumps(
                        [n.strip() for n in names_raw.split(",") if n.strip()],
                        ensure_ascii=False,
                    )

                    # norad_follow_id differs from its own NORAD →
                    # this is a provisional-NORAD remnant satellite.
                    follow_raw = sat.get("norad_follow_id")
                    norad_follow = int(follow_raw) if follow_raw else None
                    is_remnant = bool(norad_follow and norad_follow != norad)

                    existing = self._conn.execute(
                        "SELECT norad_cat_id FROM satellites WHERE norad_cat_id = ?",
                        (norad,),
                    ).fetchone()

                    if existing:
                        if is_remnant:
                            self._conn.execute(
                                "UPDATE satellites SET name = ?, status = ?,"
                                " alt_names = ?, is_hidden = 2, updated_at = ?"
                                " WHERE norad_cat_id = ?",
                                (name, status, alt_names_json, now, norad),
                            )
                            # Propagate status to the official NORAD if it is still 'unknown'
                            if norad_follow is not None and status in ("alive", "dead"):
                                self._conn.execute(
                                    "UPDATE satellites SET status = ?, updated_at = ?"
                                    " WHERE norad_cat_id = ? AND status = 'unknown'",
                                    (status, now, norad_follow),
                                )
                            # Merge remnant's names into the official NORAD's alt_names.
                            # SatNOGS stores aliases (e.g. "FO-126") only on the remnant
                            # record, so the official NORAD would otherwise never get them.
                            if norad_follow is not None and names_raw:
                                official_row = self._conn.execute(
                                    "SELECT alt_names FROM satellites WHERE norad_cat_id = ?",
                                    (norad_follow,),
                                ).fetchone()
                                if official_row is not None:
                                    existing_alt: list[str] = json.loads(
                                        official_row["alt_names"] or "[]"
                                    )
                                    new_names = [
                                        n.strip() for n in names_raw.split(",") if n.strip()
                                    ]
                                    merged = list(dict.fromkeys(existing_alt + new_names))
                                    self._conn.execute(
                                        "UPDATE satellites SET alt_names = ?, updated_at = ?"
                                        " WHERE norad_cat_id = ?",
                                        (json.dumps(merged, ensure_ascii=False), now, norad_follow),
                                    )
                            # Run full migration pipeline: migrate TLE, transmitters,
                            # is_favorite, and set satnogs_source_id on the official satellite.
                            if norad_follow is not None:
                                self._run_migration_pipeline(norad, norad_follow)
                        else:
                            self._conn.execute(
                                "UPDATE satellites SET name = ?, status = ?,"
                                " alt_names = ?, updated_at = ? WHERE norad_cat_id = ?",
                                (name, status, alt_names_json, now, norad),
                            )
                            # A satellite can be auto-hidden (is_hidden=2) if an
                            # earlier fetch_provisional_tles() run saw it as
                            # status='unknown'/'dead' with no TLE yet — a common
                            # transient state right around launch. Once SATNOGS
                            # reports it alive again, un-hide it so it isn't stuck
                            # invisible forever. is_hidden=1 (user-hidden) is left
                            # untouched — that's a deliberate user choice.
                            if status == "alive":
                                self._conn.execute(
                                    "UPDATE satellites SET is_hidden = 0"
                                    " WHERE norad_cat_id = ? AND is_hidden = 2",
                                    (norad,),
                                )
                        stats["updated"] += 1
                    else:
                        # Satellite not yet in DB (fresh install or new launch).
                        # Insert it so TLE fetch and transmitter sync can reference it.
                        # Provisional-NORAD remnants (is_remnant=True) are hidden immediately;
                        # regular satellites start visible (is_hidden=0).
                        hidden = 2 if is_remnant else 0
                        self._conn.execute(
                            "INSERT OR IGNORE INTO satellites"
                            " (norad_cat_id, name, alt_names, status, is_hidden, updated_at)"
                            " VALUES (?, ?, ?, ?, ?, ?)",
                            (norad, name, alt_names_json, status, hidden, now),
                        )
                        stats["updated"] += 1

                    total_processed += 1
                    if progress_callback:
                        progress_callback(total_processed)

        # Auto-hide orphan satellites with 0 transmitters and status='unknown'.
        self._conn.execute(
            """
            UPDATE satellites SET is_hidden = 2
            WHERE norad_cat_id NOT IN (SELECT DISTINCT norad_cat_id FROM transmitters)
            AND status = 'unknown'
            AND is_hidden = 0
            """
        )

        self._conn.commit()
        self._log_sync("satnogs_names", stats)
        return stats

    def _log_sync(self, sync_type: str, stats: dict[str, int]) -> None:
        self._conn.execute(
            """
            INSERT INTO sync_log (sync_type, started_at, finished_at, status, records_updated)
            VALUES (?, ?, ?, 'success', ?)
        """,
            (
                sync_type,
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
                stats.get("inserted", 0) + stats.get("updated", 0),
            ),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Export / Import
    # ------------------------------------------------------------------ #

    def export_manual_transmitters(self) -> list[dict[str, Any]]:
        """Return manually added transponders in a JSON-serializable format"""
        rows = self._conn.execute("""
            SELECT * FROM transmitters WHERE source = 'manual'
        """).fetchall()
        return [dict(r) for r in rows]

    def import_transmitters(self, data: list[dict[str, Any]]) -> int:
        """
        Import transponders from JSON.
        Existing UUIDs are overwritten (upsert).
        """
        now = datetime.now(UTC).isoformat()
        count = 0
        for item in data:
            item.setdefault("source", "manual")
            item.setdefault("manual_override", 1)
            item["updated_at"] = now
            cols = ", ".join(item.keys())
            placeholders = ", ".join("?" * len(item))
            self._conn.execute(
                f"INSERT OR REPLACE INTO transmitters ({cols}) VALUES ({placeholders})",
                list(item.values()),
            )
            count += 1
        self._conn.commit()
        return count
