"""KNACKSAT-2 (HS0K) beacon decoding from src/data/telemetry_formats/67683.json."""

from __future__ import annotations

import pytest

from comms.telemetry.decoder import decode_telemetry, list_formats

# A beacon payload received 2026-10-01 07:24 UTC (149 bytes, after the AX.25 header).
BEACON = bytes.fromhex(
    "0000550100000100eb0002000018010000000000000000006f12833b0681153d8fc2f53c"
    "b81e853d39b4483d0000000000000000cdcccc3c6666263e0000003e3333933e3333d33e"
    "0000000000000000ae478c40ec518c40b81e8b40b81e8b40ecd1034100001a01006d0100"
    "00494100004c410000494100004841cdcc03418941803e0ad70341c9767ebeee7800001d"
    "010142434e"
)


def _values(payload: bytes) -> dict[str, float | str]:
    frame = decode_telemetry("HS0K", payload, 67683)
    return {f.name: (f.unit if f.is_string else f.scaled_value) for f in frame.fields}


def test_beacon_decodes_every_field() -> None:
    frame = decode_telemetry("HS0K", BEACON, 67683)
    assert frame.satellite_name == "KNACKSAT-2"
    assert len(frame.fields) == 49
    v = _values(BEACON)
    assert v["ident"] == "BCN"
    assert v["batt_vout"] == pytest.approx(8.2375, abs=1e-3)
    assert v["batt_iin"] == pytest.approx(-0.2485, abs=1e-3)
    assert v["batt_temp_1"] == pytest.approx(12.75)
    assert v["batt_heater_cnt"] == 365
    assert v["mppt_vbus_2"] == pytest.approx(4.3837, abs=1e-3)
    assert v["mppt_power_6"] == pytest.approx(0.4125, abs=1e-3)
    assert v["mppt_isens_5"] == pytest.approx(0.065, abs=1e-3)
    assert (v["ant_isis"], v["ant_lora"]) == (238, 120)


def test_other_frame_types_are_not_decoded_as_a_beacon() -> None:
    other = bytes.fromhex("405a") + bytes(range(60))  # e.g. a file-name reply
    assert decode_telemetry("HS0K", other, 67683).fields == []


def test_format_maps_callsign_to_satellite() -> None:
    assert any(f["callsign"] == "HS0K" and f["norad"] == 67683 for f in list_formats())


def test_beacon_has_a_decoded_fields_sub_tab() -> None:
    from comms.telemetry.decoder import BEACON_ID, get_telemetry_id_defs  # noqa: PLC0415

    defs = get_telemetry_id_defs(67683)
    assert defs is not None
    assert list(defs) == [BEACON_ID]
    assert len(defs[BEACON_ID]["fields"]) == 49
    assert decode_telemetry("HS0K", BEACON, 67683).telemetry_id == BEACON_ID
