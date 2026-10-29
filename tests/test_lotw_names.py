"""LoTW satellite IDs written to ADIF logs."""

from __future__ import annotations

from data.lotw_names import is_lotw_satellite, lotw_sat_id


def test_sonate2_is_logged_under_lotws_id_sonate() -> None:
    sat_id = lotw_sat_id(59112, "SONATE-2", None)
    assert sat_id == "SONATE"
    assert is_lotw_satellite(sat_id)


def test_the_iss_keeps_its_ariss_id() -> None:
    assert lotw_sat_id(25544, "ISS", None) == "ARISS"
