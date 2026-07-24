"""Unit tests for comms/mode_detection.py.

is_ax25_telemetry_transmitter() is a pure function (no DB). get_norads_for_tab()
needs a satellites/transmitters schema — built directly here since
database.py's full schema setup isn't needed for this narrow join.
"""

from __future__ import annotations

import sqlite3

import pytest

from comms.mode_detection import (
    get_norads_for_tab,
    is_ax25_telemetry_transmitter,
    is_ax100_digi_transmitter,
    pick_preferred_transponder_index,
)

# ---------------------------------------------------------------------------
# is_ax25_telemetry_transmitter()
# ---------------------------------------------------------------------------


def test_afsk_mode_matches_regardless_of_baud_or_description() -> None:
    assert is_ax25_telemetry_transmitter({"mode": "AFSK", "baud": None, "description": ""})
    assert is_ax25_telemetry_transmitter({"mode": "afsk", "baud": 1200, "description": "Beacon"})


def test_4800_with_ax25_in_description_matches() -> None:
    xpdr = {"mode": "GMSK", "baud": 4800, "description": "Mode U - GMSK4k8 - AX.25"}
    assert is_ax25_telemetry_transmitter(xpdr)


def test_9600_with_ax25_no_dot_matches() -> None:
    xpdr = {"mode": "FSK", "baud": 9600, "description": "9k6 AX25 Telemetry"}
    assert is_ax25_telemetry_transmitter(xpdr)


def test_4800_without_ax25_mention_does_not_match() -> None:
    """Baud rate alone isn't a reliable signal — some satellites run
    non-AX.25 protocols at the same rates (unlike 1200, which SATNOGS's
    "AFSK" mode tag reliably identifies as AX.25 on its own)."""
    xpdr = {"mode": "GMSK", "baud": 4800, "description": "Mode U - GMSK Telemetry"}
    assert not is_ax25_telemetry_transmitter(xpdr)


def test_1200_baud_without_afsk_mode_does_not_match() -> None:
    xpdr = {"mode": "FM", "baud": 1200, "description": "Some AX.25 beacon"}
    assert not is_ax25_telemetry_transmitter(xpdr)


def test_unrelated_transmitter_does_not_match() -> None:
    xpdr = {"mode": "USB", "baud": None, "description": "Linear transponder"}
    assert not is_ax25_telemetry_transmitter(xpdr)


# ---------------------------------------------------------------------------
# get_norads_for_tab("telemetry")
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE satellites (norad_cat_id INTEGER PRIMARY KEY, name TEXT, is_hidden INTEGER)"
    )
    c.execute(
        """CREATE TABLE transmitters (
            uuid TEXT PRIMARY KEY, norad_cat_id INTEGER, description TEXT,
            mode TEXT, baud INTEGER, alive INTEGER
        )"""
    )
    return c


def _add_sat(conn: sqlite3.Connection, norad: int, name: str, hidden: int = 0) -> None:
    conn.execute(
        "INSERT INTO satellites (norad_cat_id, name, is_hidden) VALUES (?, ?, ?)",
        (norad, name, hidden),
    )


def _add_xmit(
    conn: sqlite3.Connection,
    uuid: str,
    norad: int,
    *,
    description: str = "",
    mode: str = "",
    baud: int | None = None,
    alive: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO transmitters (uuid, norad_cat_id, description, mode, baud, alive) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (uuid, norad, description, mode, baud, alive),
    )


def test_get_norads_includes_afsk_transmitter(conn: sqlite3.Connection) -> None:
    _add_sat(conn, 25544, "ISS")
    _add_xmit(conn, "u1", 25544, mode="AFSK", baud=1200)
    assert get_norads_for_tab(conn, "telemetry") == [25544]


def test_get_norads_includes_gmsk_4800_with_ax25_description(conn: sqlite3.Connection) -> None:
    _add_sat(conn, 68796, "ARICA-2")
    _add_xmit(conn, "u1", 68796, mode="GMSK", baud=4800, description="GMSK4k8 - AX.25")
    assert get_norads_for_tab(conn, "telemetry") == [68796]


def test_get_norads_excludes_hidden_satellite(conn: sqlite3.Connection) -> None:
    """A satellite whose transmitter still says alive=1 but the satellite
    itself was hidden (e.g. decayed, auto-cleaned) must not appear —
    matches the rationale already established for the other tabs."""
    _add_sat(conn, 47311, "Maya-2", hidden=2)
    _add_xmit(conn, "u1", 47311, mode="AFSK", baud=1200)
    assert get_norads_for_tab(conn, "telemetry") == []


def test_get_norads_excludes_dead_transmitter(conn: sqlite3.Connection) -> None:
    _add_sat(conn, 40908, "LilacSat-2")
    _add_xmit(conn, "u1", 40908, mode="AFSK", baud=1200, alive=0)
    assert get_norads_for_tab(conn, "telemetry") == []


def test_get_norads_excludes_non_matching_transmitter(conn: sqlite3.Connection) -> None:
    _add_sat(conn, 99999, "Linear Sat")
    _add_xmit(conn, "u1", 99999, mode="USB", baud=None)
    assert get_norads_for_tab(conn, "telemetry") == []


def test_get_norads_empty_for_unconfigured_tab_key(conn: sqlite3.Connection) -> None:
    assert get_norads_for_tab(conn, "cw") == []
    assert get_norads_for_tab(conn, "no-such-tab") == []


# ---------------------------------------------------------------------------
# is_ax100_digi_transmitter() / get_norads_for_tab("ax100digi")
# ---------------------------------------------------------------------------


def test_ax100_digi_matches_only_marmotsat_vhf_digipeater() -> None:
    marmotsat_digi = {
        "norad_cat_id": 98272,
        "description": "Mode V - AFSK1k2 - APRS Digipeater",
    }
    assert is_ax100_digi_transmitter(marmotsat_digi)

    greencube_digi = {"norad_cat_id": 53106, "description": "Digipeater"}
    assert not is_ax100_digi_transmitter(greencube_digi)  # GreenCube — excluded

    assert not is_ax100_digi_transmitter({"norad_cat_id": None})


def test_ax100_digi_matches_mode_v_wording_without_digipeater_text() -> None:
    """User-confirmed live SATNOGS wording (2026-07): "Mode V" alone must
    match even without the literal word "Digipeater" (SATNOGS descriptions
    are community-edited and had since diverged from the "...APRS
    Digipeater" wording this matcher originally assumed)."""
    xpdr = {"norad_cat_id": 98272, "description": "Mode V - AFSK1k2"}
    assert is_ax100_digi_transmitter(xpdr)


def test_ax100_digi_auto_select_picks_mode_v_not_first_by_frequency() -> None:
    """Reproduces main_window._on_comms_satellite_requested()'s selection
    logic (`next((i for i, t in enumerate(transmitters) if
    matcher(t)), 0)`) against MARMOTSat's real 6 transmitters in the exact
    downlink-ascending order Radio Control's combo lists them in (2026-07
    screenshot bug report: this used to fall through to index 0 — the
    29.410 MHz HF LFM Sounder — because _refresh_radio_control()'s SQL
    query didn't select norad_cat_id at all, so every transmitter dict's
    norad_cat_id was missing and the matcher rejected all of them)."""
    transmitters = [
        {"norad_cat_id": 98272, "description": "HF Ionospheric LFM Sounder"},
        {"norad_cat_id": 98272, "description": "HF DVB-S2"},
        {"norad_cat_id": 98272, "description": "HF CW Telemetry Beacon"},
        {"norad_cat_id": 98272, "description": "Mode V - AFSK1k2 - APRS Digipeater"},
        {"norad_cat_id": 98272, "description": "VHF CW TLM"},
        {"norad_cat_id": 98272, "description": "Mode U - Transmitter"},
    ]
    best_idx = next((i for i, t in enumerate(transmitters) if is_ax100_digi_transmitter(t)), 0)
    assert best_idx == 3
    assert transmitters[best_idx]["description"] == "Mode V - AFSK1k2 - APRS Digipeater"


# ---------------------------------------------------------------------------
# pick_preferred_transponder_index()
# ---------------------------------------------------------------------------


def test_pick_preferred_transponder_index_prefers_community_over_satnogs() -> None:
    """MARMOTSat has both a stale SATNOGS "Mode V" entry (mode=AFSK, no
    uplink) and a corrected community_transmitters.json entry (source=
    'community', USB, up=down=145.875 MHz). Both match
    is_ax100_digi_transmitter() (norad + "Digipeater"/"Mode V" in
    description), so the community one must win regardless of which one
    the DB query happens to list first."""
    transmitters_satnogs_first = [
        {
            "norad_cat_id": 98272,
            "description": "Mode V - AFSK1k2 - APRS Digipeater",
            "source": "satnogs",
        },
        {
            "norad_cat_id": 98272,
            "description": "AX100 Digipeater (GreenCube-compatible, SSB) — community",
            "source": "community",
        },
    ]
    idx = pick_preferred_transponder_index(transmitters_satnogs_first, is_ax100_digi_transmitter)
    assert idx == 1

    transmitters_community_first = list(reversed(transmitters_satnogs_first))
    idx2 = pick_preferred_transponder_index(transmitters_community_first, is_ax100_digi_transmitter)
    assert idx2 == 0


def test_pick_preferred_transponder_index_falls_back_when_no_community_match() -> None:
    transmitters = [
        {"norad_cat_id": 98272, "description": "HF DVB-S2", "source": "satnogs"},
        {
            "norad_cat_id": 98272,
            "description": "Mode V - AFSK1k2 - APRS Digipeater",
            "source": "satnogs",
        },
    ]
    idx = pick_preferred_transponder_index(transmitters, is_ax100_digi_transmitter)
    assert idx == 1


def test_pick_preferred_transponder_index_returns_none_when_nothing_matches() -> None:
    transmitters = [{"norad_cat_id": 98272, "description": "HF DVB-S2", "source": "satnogs"}]
    assert pick_preferred_transponder_index(transmitters, is_ax100_digi_transmitter) is None


def test_ax100_digi_excludes_marmotsats_other_transmitters() -> None:
    """MARMOTSat carries several transmitters under the same NORAD id (HF
    CW beacon/DVB-S2/LFM sounder at 29.410 MHz, VHF CW telemetry at
    145.875 MHz) — only the VHF digipeater one should match, since
    _on_comms_satellite_requested() auto-selects the first match."""
    for description in (
        "HF CW Telemetry Beacon",
        "HF DVB-S2",
        "HF Ionospheric LFM Sounder",
        "VHF CW TLM",
    ):
        xpdr = {"norad_cat_id": 98272, "description": description}
        assert not is_ax100_digi_transmitter(xpdr), description


def test_get_norads_ax100_digi_includes_marmotsat(conn: sqlite3.Connection) -> None:
    _add_sat(conn, 98272, "MARMOTSat")
    _add_xmit(
        conn, "u1", 98272, mode="AFSK", baud=1200, description="Mode V - AFSK1k2 - APRS Digipeater"
    )
    assert get_norads_for_tab(conn, "ax100digi") == [98272]


def test_get_norads_ax100_digi_excludes_marmotsats_hf_transmitters(
    conn: sqlite3.Connection,
) -> None:
    _add_sat(conn, 98272, "MARMOTSat")
    _add_xmit(conn, "u1", 98272, mode="CW", description="HF CW Telemetry Beacon")
    _add_xmit(conn, "u2", 98272, mode="DVB-S2", baud=33000, description="HF DVB-S2")
    assert get_norads_for_tab(conn, "ax100digi") == []


def test_get_norads_ax100_digi_excludes_greencube(conn: sqlite3.Connection) -> None:
    """GreenCube (IO-117) uses the same AX100 framing but is out of service
    and intentionally excluded (2026-07 user request)."""
    _add_sat(conn, 53106, "GreenCube")
    _add_xmit(conn, "u1", 53106, mode="GMSK", baud=1200, description="Digipeater")
    assert get_norads_for_tab(conn, "ax100digi") == []
