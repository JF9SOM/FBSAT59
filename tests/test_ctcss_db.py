"""resolve_ctcss(): the satellite-wide tone table must not tag every transmitter."""

from __future__ import annotations

from data.ctcss_db import resolve_ctcss

ISS = 25544
SO50 = 27607


def _xpdr(ul: float | None, dl: float | None, tone: float | None = None) -> dict[str, object]:
    return {"uplink_low": ul, "downlink_low": dl, "ctcss_tone": tone}


def test_iss_aprs_same_band_gets_no_tone() -> None:
    # Mode V APRS: 145.825 MHz up and down, no CTCSS in SATNOGS.
    assert resolve_ctcss(ISS, _xpdr(145_825_000, 145_825_000)) == (None, None)


def test_iss_crew_vv_fm_same_band_gets_no_tone() -> None:
    assert resolve_ctcss(ISS, _xpdr(144_490_000, 145_800_000)) == (None, None)


def test_iss_cross_band_repeater_falls_back_to_the_table() -> None:
    assert resolve_ctcss(ISS, _xpdr(145_990_000, 437_800_000)) == (67.0, None)


def test_downlink_only_transmitter_never_gets_a_tone() -> None:
    assert resolve_ctcss(ISS, _xpdr(None, 437_800_000)) == (None, None)
    assert resolve_ctcss(ISS, _xpdr(0, 437_800_000)) == (None, None)


def test_own_satnogs_tone_always_wins() -> None:
    same_band = resolve_ctcss(ISS, _xpdr(145_825_000, 145_825_000, 88.5))
    assert same_band == (88.5, None)  # explicit transmitter data is honoured
    cross_band = resolve_ctcss(ISS, _xpdr(145_990_000, 437_800_000, 88.5))
    assert cross_band == (88.5, None)  # and beats the table's 67.0


def test_activation_tone_follows_the_same_rule() -> None:
    assert resolve_ctcss(SO50, _xpdr(145_850_000, 436_795_000)) == (67.0, 74.4)
    assert resolve_ctcss(SO50, _xpdr(145_850_000, 145_900_000)) == (None, None)
    assert resolve_ctcss(SO50, _xpdr(None, 436_795_000)) == (None, None)


def test_satellite_not_in_the_table_has_no_fallback() -> None:
    assert resolve_ctcss(99999, _xpdr(145_990_000, 437_800_000)) == (None, None)
    assert resolve_ctcss(None, _xpdr(145_990_000, 437_800_000)) == (None, None)
    assert resolve_ctcss(99999, _xpdr(145_990_000, 437_800_000, 71.9)) == (71.9, None)


def test_uplink_without_a_known_downlink_keeps_the_fallback() -> None:
    assert resolve_ctcss(ISS, _xpdr(145_990_000, None)) == (67.0, None)
