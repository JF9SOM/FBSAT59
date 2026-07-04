"""Shared classification rules for Communications tabs.

Determines which transmitters/satellites belong to which Communications tab
(FT4, APRS, SSTV, CW, ...). Both `RadioControlWidget`'s auto-open-on-select
logic and the "Comms Quick Panel" per-tab satellite filter call the same
functions here, so the two can never silently drift apart.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


def is_ft4_transmitter(xpdr: dict[str, Any]) -> bool:
    """Match FT4 only — not generic FT8.

    FT4 (6s slots) and FT8 (15s slots) are different WSJT-X protocols; the
    app's FT4 tab (Ft4Scheduler/Ft4Codec) only handles the 6s cadence, so a
    bare "FT8" transponder (e.g. a rocket upper-stage payload beacon) would
    never actually decode there. Broadening this to "FT8" also pulled in
    stray SATNOGS transmitters unrelated to the 3 known FT4-calling
    satellites (see get_norads_for_tab's callers).
    """
    desc = (xpdr.get("description") or "").upper()
    return "FT4" in desc


def is_aprs_transmitter(xpdr: dict[str, Any]) -> bool:
    desc = (xpdr.get("description") or "").upper()
    mode = (xpdr.get("mode") or "").upper()
    return "APRS" in desc or mode == "AFSK"


def is_sstv_transmitter(xpdr: dict[str, Any]) -> bool:
    desc = (xpdr.get("description") or "").upper()
    return "SSTV" in desc or "SSDV" in desc or "IMAGING" in desc


def is_cw_transmitter(xpdr: dict[str, Any]) -> bool:
    mode = (xpdr.get("mode") or "").upper()
    return mode in ("CW", "CW-R")


@dataclass(frozen=True)
class CommsTabConfig:
    """Per-tab display configuration for the Comms Quick Panel (right-side panel).

    show_input_source: whether the satellite quick-select combo is shown.
        Off for tabs where a mode/description filter would not narrow the
        list usefully (CW: too many satellites carry it) or does not apply
        to the tab's model at all (Q65: not tied to a transponder; Telemetry
        and METEOR: already have their own dedicated in-tab selector).
    freq_source: where the mirrored DL/UL frequency readout comes from.
        "radio_control" mirrors RadioControlWidget's Doppler-corrected
        downlink/uplink labels. "satdump" reads the METEOR tab's own fixed
        receive frequency (SatDump does not follow the selected transponder).
        None hides the frequency readout entirely (Q65: the selected
        transponder, if any, has no relation to the EME band in use).
    matcher: identifies which transmitters belong to this tab, reused from
        RadioControlWidget._check_comms_auto_open(). None for tabs that do
        not use a mode/description filter (their satellite list, if any, is
        built some other way).
    """

    show_input_source: bool
    freq_source: str | None
    matcher: Callable[[dict[str, Any]], bool] | None = None


COMMS_TAB_CONFIG: dict[str, CommsTabConfig] = {
    "ft4": CommsTabConfig(
        show_input_source=True, freq_source="radio_control", matcher=is_ft4_transmitter
    ),
    "aprs": CommsTabConfig(
        show_input_source=True, freq_source="radio_control", matcher=is_aprs_transmitter
    ),
    "sstv": CommsTabConfig(
        show_input_source=True, freq_source="radio_control", matcher=is_sstv_transmitter
    ),
    "cw": CommsTabConfig(show_input_source=False, freq_source="radio_control"),
    "q65": CommsTabConfig(show_input_source=False, freq_source=None),
    "meteor": CommsTabConfig(show_input_source=False, freq_source="satdump"),
    "telemetry": CommsTabConfig(show_input_source=False, freq_source="radio_control"),
}


def get_norads_for_tab(conn: sqlite3.Connection, tab_key: str) -> list[int]:
    """Return NORAD ids of satellites carrying a transmitter matching *tab_key*.

    Returns an empty list for tab keys with no matcher configured (or unknown
    tab keys) — those tabs do not offer a quick-select satellite filter.

    Joins against satellites.is_hidden = 0 so a satellite that was hidden by
    the TLE cleanup / provisional-ID migration pipelines (e.g. a decayed
    object whose stale transmitter row is still marked alive=1) can never
    appear in the combo — the satellite list has nothing to select there anyway.
    """
    config = COMMS_TAB_CONFIG.get(tab_key)
    if config is None or config.matcher is None:
        return []
    rows = conn.execute(
        """
        SELECT DISTINCT t.norad_cat_id, t.description, t.mode
        FROM transmitters t
        JOIN satellites s ON s.norad_cat_id = t.norad_cat_id
        WHERE t.alive = 1 AND s.is_hidden = 0
        """
    ).fetchall()
    matcher = config.matcher
    norads: set[int] = set()
    for row in rows:
        xpdr = {"description": row["description"], "mode": row["mode"]}
        if matcher(xpdr):
            norads.add(int(row["norad_cat_id"]))
    return sorted(norads)
