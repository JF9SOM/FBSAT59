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


_MARMOTSAT_NORAD_ID = 98272


def is_ax100_digi_transmitter(xpdr: dict[str, Any]) -> bool:
    """MARMOTSat's VHF digipeater (145.875 MHz) only.

    Restricted by NORAD id (GreenCube (IO-117) uses the same AX100
    "ASM+Golay" GMSK framing and would otherwise also match, but its
    ground station is currently out of service, so it is intentionally
    excluded — 2026-07 user request) *and* by description text: MARMOTSat
    carries several other transmitters on the same NORAD id (HF CW beacon,
    HF DVB-S2, HF LFM sounder, all at 29.410 MHz; VHF CW telemetry at
    145.875 MHz) that a norad-only check would also match, and
    main_window._on_comms_satellite_requested() auto-selects the *first*
    transponder this matcher accepts — a description check is needed to
    land on the 145.875 MHz digipeater specifically instead of one of the
    29.410 MHz entries.

    "MODE V" is the confirmed live SATNOGS wording for this transmitter
    (user-verified 2026-07); "DIGIPEATER" is also accepted since that
    matched an earlier (possibly since-edited) SATNOGS description and is
    otherwise harmless to keep — SATNOGS descriptions are community-edited
    and can change wording over time.
    """
    if xpdr.get("norad_cat_id") != _MARMOTSAT_NORAD_ID:
        return False
    desc = (xpdr.get("description") or "").upper()
    return "MODE V" in desc or "DIGIPEATER" in desc


def is_ax25_telemetry_transmitter(xpdr: dict[str, Any]) -> bool:
    """AX.25-framed transmitters the Telemetry tab's Direwolf (AX.25) mode
    can decode: 1200 baud Bell 202 AFSK, or 4800/9600 baud G3RUH-style
    scrambled FSK/GMSK (see comms.aprs.direwolf / comms.aprs.g3ruh_demod).

    mode == "AFSK" is trusted on its own — same signal is_aprs_transmitter()
    relies on — since SATNOGS uses it specifically for 1200 baud AX.25.
    4800/9600 are NOT AFSK (a different modulation entirely: G3RUH
    scrambled FSK or GMSK), so baud rate alone isn't a reliable signal at
    those speeds either — some satellites run non-AX.25 protocols at the
    same rates — this also requires an explicit "AX.25" marker in the
    transmitter description (e.g. SATNOGS's own "GMSK4k8 - AX.25" for
    ARICA-2).
    """
    mode = (xpdr.get("mode") or "").upper()
    if mode == "AFSK":
        return True
    if xpdr.get("baud") not in (4800, 9600):
        return False
    desc = (xpdr.get("description") or "").upper()
    return "AX.25" in desc or "AX25" in desc


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
    show_rig_buttons: whether the Rig 1 / Rig 2 connect-proxy buttons are
        shown. Off for METEOR: that tab's only SDR connection point is its
        own in-tab "SDR接続" button (Rig 1/2 there would just connect an
        unrelated Hamlib/SDR slot and confuse users into thinking it drives
        SatDump). The Rotator button stays on regardless — antenna tracking
        still goes through the normal Rig 1/2 rotator plumbing.
    """

    show_input_source: bool
    freq_source: str | None
    matcher: Callable[[dict[str, Any]], bool] | None = None
    show_rig_buttons: bool = True


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
    "meteor": CommsTabConfig(
        show_input_source=False, freq_source="satdump", show_rig_buttons=False
    ),
    "telemetry": CommsTabConfig(
        show_input_source=False,
        freq_source="radio_control",
        matcher=is_ax25_telemetry_transmitter,
    ),
    "ax100digi": CommsTabConfig(
        show_input_source=True, freq_source="radio_control", matcher=is_ax100_digi_transmitter
    ),
}


def pick_preferred_transponder_index(
    transmitters: list[dict[str, Any]], matcher: Callable[[dict[str, Any]], bool]
) -> int | None:
    """Return the index of the best transponder in *transmitters* matching
    *matcher*, preferring a manually-curated source='community' entry over
    any other matching source (typically SATNOGS).

    Some satellites have both a corrected community entry and a stale/
    incomplete SATNOGS one for the same protocol — e.g. MARMOTSat's SATNOGS
    transmitter lists its AX100 digipeater as mode=AFSK with no uplink
    (satellite too newly launched for SATNOGS to have it right yet), while
    community_transmitters.json has the corrected SSB mode + up/down
    frequencies. Both match is_ax100_digi_transmitter(), so a plain
    first-match search would non-deterministically land on whichever sorts
    first in the DB query. Returns None if nothing matches.
    """
    matches = [i for i, t in enumerate(transmitters) if matcher(t)]
    if not matches:
        return None
    community_matches = [i for i in matches if transmitters[i].get("source") == "community"]
    return community_matches[0] if community_matches else matches[0]


def get_norads_for_tab(conn: sqlite3.Connection, tab_key: str) -> list[int]:
    """Return NORAD ids of satellites carrying a transmitter matching *tab_key*.

    Returns an empty list for tab keys with no matcher configured (or unknown
    tab keys) — those tabs do not offer a quick-select satellite filter.

    Joins against satellites.is_hidden = 0 so a satellite that was hidden by
    the TLE cleanup / provisional-ID migration pipelines (e.g. a decayed
    object whose stale transmitter row is still marked alive=1) can never
    appear in the combo — the satellite list has nothing to select there anyway.

    telemetry's show_input_source is False (its Quick Panel doesn't show a
    satellite combo — the tab has its own), but its matcher is still set so
    TelemetryTab._populate_afsk_combo() can call this function directly to
    find AX.25-capable satellites beyond its 10 hand-written format
    definitions, e.g. 4800/9600 baud ones without field-level decode.
    """
    config = COMMS_TAB_CONFIG.get(tab_key)
    if config is None or config.matcher is None:
        return []
    rows = conn.execute(
        """
        SELECT DISTINCT t.norad_cat_id, t.description, t.mode, t.baud
        FROM transmitters t
        JOIN satellites s ON s.norad_cat_id = t.norad_cat_id
        WHERE t.alive = 1 AND s.is_hidden = 0
        """
    ).fetchall()
    matcher = config.matcher
    norads: set[int] = set()
    for row in rows:
        xpdr = {
            "description": row["description"],
            "mode": row["mode"],
            "baud": row["baud"],
            "norad_cat_id": row["norad_cat_id"],
        }
        if matcher(xpdr):
            norads.add(int(row["norad_cat_id"]))
    return sorted(norads)
