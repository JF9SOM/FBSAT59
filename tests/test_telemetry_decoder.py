"""
Unit tests for comms/telemetry/decoder.py.

No network connection required; decodes fixed byte strings against the
JSON format files already checked into src/data/telemetry_formats/.
"""

from __future__ import annotations

import pytest

from comms.telemetry.decoder import (
    TelemetryField,
    TelemetryFrame,
    decode_telemetry,
    get_telemetry_id_defs,
)

# A real OrigamiSat-2 (NORAD 68795) ID130 frame received 2026-09-13, stored
# in the telemetry_log table. Values below were cross-checked against a
# from-scratch struct.unpack() decode built directly from the official
# ORI-2-0027-OPR spec (ver. 2026-04-21).
_ORIGAMISAT2_ID130_HEX = (
    "ccfe820c6aa1e2d6fd000006010000000b6b010101004142c736b9de75f54200666740"
    "a651ec3fb3333340a65c2900000000413510c1090500000100dcefc6c4c43c3d19213a"
    "753fa73c8b618c3c22324e3a5f0ae03c72bb5c46a8e97ac6ea60a846deb36846a22514c"
    "6e9e0d746e7886700000165bf18009c3e41bf233f1ed45abef3ca2ebebebbc3becd5822"
    "3f563df4413c22657c73cc85414408651706196f4157531e10b11c3ec09f17bf8b8e94e"
    "bc0b96cbdbfb4363140aa5f8dcf30f66d0000000000000000000000009993"
)


def test_get_telemetry_id_defs_origamisat2() -> None:
    # OrigamiSat-2 has both an AX.25 telemetry_ids schema (65/100/130) and a
    # cw_frames schema ("TLM", the CW beacon) -- get_telemetry_id_defs() merges
    # both so the "Decoded Fields" tabs work regardless of which mode a frame
    # came in on.
    id_defs = get_telemetry_id_defs(68795)
    assert id_defs is not None
    assert set(id_defs.keys()) == {"1", "65", "100", "130", "TLM"}


def test_get_telemetry_id_defs_missing_norad_returns_none() -> None:
    assert get_telemetry_id_defs(1) is None


def test_get_telemetry_id_defs_old_flat_schema_returns_none() -> None:
    # LilacSat-2 (40908) still uses the older single flat "fields" list —
    # it must not be reported as having a telemetry_ids schema.
    assert get_telemetry_id_defs(40908) is None


def test_decode_origamisat2_id130_frame() -> None:
    payload = bytes.fromhex(_ORIGAMISAT2_ID130_HEX)
    tf = decode_telemetry("JS1YRU", payload, norad=68795)

    assert tf.telemetry_id == 130
    assert tf.has_fields

    values = {f.name: f.scaled_value for f in tf.fields}
    is_integer = {f.name: f.is_integer for f in tf.fields}
    assert values["telemetry_id"] == 130
    assert values["adcs_mode"] == 1
    assert values["onboard_unix_time"] == 1788994262

    # Plain uint8/uint16/uint32 status-and-count fields should be flagged
    # as integers (for UI display without pointless trailing zeros); the
    # physical-unit float32/float64 measurements should not be.
    assert is_integer["telemetry_id"]
    assert is_integer["onboard_unix_time"]
    assert not is_integer["quat_x"]
    assert not is_integer["pos_x"]

    # Unit quaternion: x^2+y^2+z^2+w^2 should be ~1.
    quat_sq_sum = sum(values[name] ** 2 for name in ("quat_x", "quat_y", "quat_z", "quat_w"))
    assert abs(quat_sq_sum - 1.0) < 1e-4

    # Orbit radius should be close to Earth radius + ~540 km altitude.
    orbit_radius_km = sum(values[name] ** 2 for name in ("pos_x", "pos_y", "pos_z")) ** 0.5 / 1000.0
    assert 6800 < orbit_radius_km < 7000


def test_decode_unknown_telemetry_id_falls_back_to_raw() -> None:
    # First 3 bytes: packet length, gen timing, telemetry ID=2 (not defined
    # for OrigamiSat-2 in the current format file).
    payload = bytes([0x00, 0xFF, 0x02]) + b"\x00" * 10
    tf = decode_telemetry("JS1YRU", payload, norad=68795)
    assert tf.telemetry_id == 2
    assert not tf.has_fields


def test_raw_summary_flags_truncation_when_payload_exceeds_preview() -> None:
    # 33-byte payload (66 hex chars) — longer than the 40-char/20-byte
    # preview summary() shows, so the truncation note must appear with the
    # actual shown/total hex-char counts (not a hardcoded satellite-specific
    # number).
    payload = bytes([0x00, 0xFF, 0x02]) + b"\x00" * 30
    tf = decode_telemetry("JS1YRU", payload, norad=68795)
    assert (
        tf.summary()
        == "[raw] 00ff020000000000000000000000000000000000 (40/66 hex chars — rest omitted)"
    )


def test_raw_summary_no_truncation_note_for_short_payload() -> None:
    # 10-byte payload (20 hex chars) fits entirely within the preview, so
    # no "rest omitted" note should be appended.
    tf = decode_telemetry("JS1YRU", bytes(10), norad=1)
    assert tf.summary() == "[raw] 00000000000000000000"


# -- Marina (NORAD 69920): plain comma-separated ASCII telemetry, not a --
# -- packed binary struct. Synthetic frames built from the field order in --
# -- satnogs-decoders' marina.ksy (not real received data). --


def test_get_telemetry_id_defs_marina() -> None:
    id_defs = get_telemetry_id_defs(69920)
    assert id_defs is not None
    assert set(id_defs.keys()) == {"MGS", "OBC", "PSU", "SOL", "CLS", "LOD", "U", "V", "BEACON"}


def test_decode_marina_obc_message() -> None:
    payload = b"OBC,1234,987654,1700000000,45000,32000,1,3,42,WATCHDOG"
    tf = decode_telemetry("OM9MAR", payload, norad=69920)

    assert tf.telemetry_id == "OBC"
    assert tf.has_fields

    values = {f.name: f.scaled_value for f in tf.fields}
    strings = {f.name: f.unit for f in tf.fields if f.is_string}
    assert values["obc_uptime"] == 1234
    assert values["obc_uptime_tot"] == 987654
    assert values["obc_lifetime_boot_counter"] == 42
    assert strings["obc_reset_cause"] == "WATCHDOG"


def test_decode_marina_psu_message_hex_bitflags() -> None:
    # psu_ch_state = 0x5B = 0b101_1011 -> bits 0,1,3,4,6 set; 2,5 clear.
    payload = b"PSU,2,100,200000,4050,250,300,150,5B,1,0,1"
    tf = decode_telemetry("OM9MAR", payload, norad=69920)

    values = {f.name: f.scaled_value for f in tf.fields}
    assert values["psu_ch_state_num"] == 0x5B
    assert [values[f"psu_ch{i}_state"] for i in range(7)] == [1, 1, 0, 1, 1, 0, 1]


def test_decode_marina_sol_message_nan_sentinel() -> None:
    payload = b"SOL,nan,10,11,12,13,14,20,21,22,23,24,25"
    tf = decode_telemetry("OM9MAR", payload, norad=69920)

    values = {f.name: f.scaled_value for f in tf.fields}
    assert values["sol_temp_yn"] == -32768
    assert values["sol_temp_xp"] == 10
    assert values["sol_diode_yp"] == 25


def test_decode_marina_uhf_message_rssi_scale() -> None:
    # marina.ksy: uhf_act_rssi_raw = raw/2 - 134 (dBm).
    payload = b"U,111,222,1,0,25,26,27,3,OM9MAR,50,49,40,42"
    tf = decode_telemetry("OM9MAR", payload, norad=69920)

    values = {f.name: f.scaled_value for f in tf.fields}
    is_integer = {f.name: f.is_integer for f in tf.fields}
    assert values["uhf_act_rssi_raw"] == 40 / 2 - 134
    assert values["uhf_dcd_rssi_raw"] == 42 / 2 - 134
    assert not is_integer["uhf_act_rssi_raw"]  # scaled, not a plain count


def test_decode_marina_unrecognized_prefix_falls_back_to_raw() -> None:
    tf = decode_telemetry("OM9MAR", b"XYZ,1,2,3", norad=69920)
    assert tf.telemetry_id is None
    assert not tf.has_fields


def test_decode_origamisat2_onboard_time_shown_as_date() -> None:
    # ID01 frame received 2026-10-02: onboard_unix_time 0x6A68EA82 is the
    # satellite's own clock (2026-07-28 17:44:34 UTC), not the reception time.
    payload = bytes.fromhex("72fe01236a68ea829000000104000001000120d0fe010c44")
    tf = decode_telemetry("JS1YRU", payload, norad=68795)

    assert tf.telemetry_id == 1
    by_name = {f.name: f for f in tf.fields}
    assert by_name["onboard_unix_time"].is_string
    assert by_name["onboard_unix_time"].unit == "2026-07-28 17:44:34 UTC"
    assert by_name["telemetry_count"].scaled_value == 0x23


# A real UmKA-1 (NORAD 57172) frame info field (gr-satellites output of the 2026-10-12 pass
# recording): a 0x4246 UHF beacon record followed by a 0xDE21 PS telemetry record.
_UMKA1_INFO = bytes.fromhex(
    "46420200010019000504ff6d2113250e22cccc6a7a2d0000802500002c002c1f0021de04"
    "0001002e00070004000400000000000000dbff000024000000000038021f02fa013f0200"
    "200000201f852d0000622a955aea04"
)


def test_umka1_records_are_decoded_with_the_sputnix_layout() -> None:
    assert len(_UMKA1_INFO) == 87
    tf = decode_telemetry("RS40S-1", _UMKA1_INFO, 57172)
    assert tf.record_ids == ["beacon_uhf", "telemetry_ps"]
    by_name = {f.name: f for f in tf.fields}
    assert by_name["uhf_t_amp"].scaled_value == 5
    assert by_name["uhf_rxbitrate"].scaled_value == 9600
    assert by_name["uhf_time"].unit == "2026-10-12 12:01:38 UTC"
    assert by_name["uhf_voltage"].scaled_value == 7980
    assert by_name["ps_iab"].scaled_value == -37
    assert by_name["ps_uab"].scaled_value == 7968
    assert by_name["ps_reg_tel_id"].scaled_value == 11653


def test_umka1_defs_have_one_sub_tab_per_record() -> None:
    defs = get_telemetry_id_defs(57172)
    assert defs is not None
    assert list(defs) == ["beacon_uhf", "telemetry_ps"]


# Real Lobachevsky (NORAD 67284) frames from the IQ recording of 2026-10-19 (decoded by
# gr-satellites' GEOSCAN framing, 72 bytes each). The layout matches SatNOGS' geoscan16u
# decoder; the values were checked for physical plausibility (orbit radius, speed,
# quaternion norm, satellite time against the reception time).
_LOB = {
    "GNSS-42": (
        "12984635bd42c710d151a10100000f6699cab55a71c9efde8b4aa25ea1c54bc4a1435c83aec5f0"
        "40883ee7b76dbd6586bbbe0ac763bf0000010000006717933ee2d1243ebbac713f"
    ),
    "GNSS-46": (
        "12984635bd46870000004850d56a79d275ffaecd9b00f9f8fbfb4ee47200060df2ff3e6aeeff48"
        "50d56a79d275ffaecd9b00f9f8fbfb4ee47200060df2ff3e6aeeffc30024dcc900"
    ),
    "EPS": (
        "12982d29bd52c50001234008811b0017250212063e104b1043107fb558003a88ff0f7f7f7f7f7f"
        "7f7f7f7f7f7f7f7f00000000000000000000000000000000000000000000000000"
    ),
    "BEACON": (
        "848a82869e9c60a4a67066a600e103f001027301c601be301b0707f70f3a881a0102e501c80133"
        "301b7f7f7f7f3a88c500000000000000000c1e30f6029590d2d989010015000000"
    ),
}


def _lob(kind: str) -> dict[str, object]:
    tf = decode_telemetry("", bytes.fromhex(_LOB[kind]), 67284)
    assert tf.telemetry_id == kind
    return {f.name: f for f in tf.fields}


def test_lobachevsky_frame_types_are_listed_for_the_decoded_fields_tabs() -> None:
    defs = get_telemetry_id_defs(67284)
    assert defs is not None
    assert set(defs) == {"BEACON", "EPS", "GNSS-42", "GNSS-43", "GNSS-46"}


def test_lobachevsky_gnss_42_is_a_plausible_state_vector() -> None:
    import math

    f = _lob("GNSS-42")
    assert f["posix_time"].unit == "2026-10-19 01:40:18.247 UTC"  # type: ignore[attr-defined]
    pos = [f[f"coord_{a}"].scaled_value for a in "xyz"]  # type: ignore[attr-defined]
    assert math.hypot(*pos) == __import__("pytest").approx(6.874e6, rel=1e-3)
    q = [f[f"q{i}"].scaled_value for i in range(4)]  # type: ignore[attr-defined]
    assert math.hypot(*q) == __import__("pytest").approx(1.0, abs=1e-3)


def test_lobachevsky_gnss_46_has_a_record_number_time_and_scaled_state() -> None:
    import math

    f = _lob("GNSS-46")
    assert f["num"].raw_value == 135  # type: ignore[attr-defined]
    assert f["a_time"].unit == "2026-10-18 23:03:36 UTC"  # type: ignore[attr-defined]
    pos = [f[f"a_x_{a}"].scaled_value for a in "xyz"]  # type: ignore[attr-defined]
    vel = [f[f"a_x_v{a}"].scaled_value for a in "xyz"]  # type: ignore[attr-defined]
    assert 6.8e6 < math.hypot(*pos) < 6.9e6  # an orbit radius, in metres (0.1 m units)
    assert 7.5 < math.hypot(*vel) / 1000 < 7.9  # an orbital speed in km/s


def test_lobachevsky_eps_beacon_values() -> None:
    f = _lob("EPS")
    assert [f[f"cell{i}_voltage"].scaled_value for i in (1, 2, 3)] == [4158, 4171, 4163]  # type: ignore[attr-defined]
    assert f["int_devices_mask"].raw_value == 0x0FFF  # type: ignore[attr-defined]
    assert f["ext_timekeeper"].raw_value == 1  # type: ignore[attr-defined]
    assert f["base_eps_mod"].raw_value == 2 and f["base_self_addr"].raw_value == 10  # type: ignore[attr-defined]
    assert f["temp_eps_battery"].raw_value == 127  # type: ignore[attr-defined]


def test_lobachevsky_ax25_beacon_values() -> None:
    f = _lob("BEACON")
    assert f["pid"].raw_value == 0xF0  # type: ignore[attr-defined]
    assert f["eps1_consumption_current"].raw_value == 371  # type: ignore[attr-defined]
    assert f["eps1_cell_voltage_full"].raw_value == 12478  # type: ignore[attr-defined]
    assert f["eps1_battery_temp"].raw_value == 27  # type: ignore[attr-defined]
    assert f["eps1_boot_count"].raw_value == 282  # type: ignore[attr-defined]
    assert f["comm_rssi"].raw_value == -107  # type: ignore[attr-defined]


def test_lobachevsky_unknown_system_stays_raw() -> None:
    raw = bytes.fromhex("12982d25bd4a1a01") + bytes(64)  # system 0xBD25 is not defined
    tf = decode_telemetry("", raw, 67284)
    assert tf.telemetry_id is None and not tf.has_fields


# -- LASARsat (NORAD 62391): comma-separated ASCII telemetry (SatNOGS lasarsat.ksy) --
# DOS / NAV / greeting frames are real ones from the IQ recording of 2026-10-19.
_LASAR_DOS = b"DOS,80,0,0,0,-1537,415,-570,86,256,35,119,119,116,114,169,857\x00"
_LASAR_NAV = b"NAV,9,9582244,4290989321,1537870,3403880,-10485,-12629,-9500,0,0,18,168,0\x00"


def test_lasarsat_defs_list_every_message_type() -> None:
    defs = get_telemetry_id_defs(62391)
    assert defs is not None
    assert set(defs) == {"OBC", "PSU", "SOL", "MGS", "DOS", "NAV", "U", "V", "MSG", "BEACON"}


def test_lasarsat_dos_keeps_the_last_nul_terminated_field() -> None:
    tf = decode_telemetry("OK0LSR", _LASAR_DOS, norad=62391)
    assert tf.telemetry_id == "DOS"
    by_name = {f.name: f for f in tf.fields}
    assert len(by_name) == 16
    assert by_name["dos_mag_x"].raw_value == -1537
    assert by_name["dos_uptime"].raw_value == 857


def test_lasarsat_nav_has_all_thirteen_fields() -> None:
    tf = decode_telemetry("OK0LSR", _LASAR_NAV, norad=62391)
    assert tf.telemetry_id == "NAV"
    assert len(tf.fields) == 13
    assert {f.name: f.raw_value for f in tf.fields}["nav_time"] == 9582244


def test_lasarsat_psu_channel_bits_and_sol_nan() -> None:
    psu = decode_telemetry("OK0LSR", b"PSU,44,33937,178475,8172,3065,2815,1,111,7f,1,29\x00", 62391)
    by_name = {f.name: f.raw_value for f in psu.fields}
    assert by_name["psu_ch_state_num"] == 0x7F
    assert all(by_name[f"psu_ch{i}_state"] == 1 for i in range(7))
    assert by_name["psu_gnd_wdt"] == 29
    sol = decode_telemetry(
        "OK0LSR", b"SOL,2812,2831,2812,nan,2806,2806,134,136,136,nan,134,nan\x00", 62391
    )
    sol_by_name = {f.name: f.raw_value for f in sol.fields}
    assert len(sol_by_name) == 12
    assert sol_by_name["sol_temp_zn"] == -32768


def test_lasarsat_u_with_empty_last_digipeater() -> None:
    tf = decode_telemetry(
        "OK0LSR", b"U,33792,166657,40,0,3217,3161,3081,0,,7577,387,89,60\x00", 62391
    )
    by_name = {f.name: f for f in tf.fields}
    assert len(by_name) == 13
    assert by_name["uhf_rx_cnt"].raw_value == 7577


def test_lasarsat_free_text_goes_to_the_message_tab() -> None:
    tf = decode_telemetry("OK0LSR", b"Celebrating 10 000 orbits!", norad=62391)
    assert tf.telemetry_id == "MSG"
    assert tf.fields[0].unit == "Celebrating 10 000 orbits!"


def test_lasarsat_digipeated_message_shows_the_ax25_header() -> None:
    from comms.aprs.parser import Ax25Frame

    frame = Ax25Frame(
        dest="CQ", src="OK0ABC", via=["OK0LSR*"], pid=0xF0, payload=b"Hello from the ground"
    )
    tf = decode_telemetry("OK0ABC", frame.payload, norad=62391, ax25=frame)
    assert tf.telemetry_id == "MSG"
    shown = {f.name: f.unit for f in tf.fields}
    assert shown == {
        "ax25_dest": "CQ",
        "ax25_src": "OK0ABC",
        "ax25_via": "OK0LSR*",
        "ax25_pid": "0xF0",
        "text_message": "Hello from the ground",
    }
    defs = get_telemetry_id_defs(62391)
    assert defs is not None
    assert [f["name"] for f in defs["MSG"]["fields"]][:2] == ["ax25_dest", "ax25_src"]


# --- Foresail-1p (66778): Skylink + CCSDS PUS, layout from the SatNOGS foresail1p.ksy ----------

# Two frames decoded from a real IQ recording (2026-10-18 01:32 UTC).
_FORESAIL_ADCS = bytes.fromhex(
    "664f48324631530985a6054400fa008c"
    "0b340b3400561003056ad42186000010"
    "936f4700000000000000000000000000"
    "0000000000000000000000800008800c"
    "00000000000000000000000000000000"
    "0000f401090064000900c1effd17c023"
    "19000b000d000000000000004a484e4c"
)
_FORESAIL_UHF = bytes.fromhex(
    "664f48324631530985a7054400fa0002"
    "0b340b34003f1003046ad42186fa6800"
    "00900005010000000000ff0700000000"
    "0000000000000000000068526b520101"
    "01fa000000fa0000000b000600c9ff08"
    "0000000000fe6fd9a4"
)


def _by_name(frame: TelemetryFrame) -> dict[str, TelemetryField]:
    return {f.name: f for f in frame.fields}


def test_foresail1p_defs_have_one_sub_tab_per_frame_type() -> None:
    defs = get_telemetry_id_defs(66778)
    assert defs is not None
    assert set(defs) == {
        "REPEATER",
        "OBC_HK",
        "EPS_HK",
        "UHF_HK",
        "ADCS_HK",
        "EVENT_1",
        "EVENT_2",
        "EVENT_3",
        "EVENT_4",
    }


def test_foresail1p_uhf_housekeeping_real_frame() -> None:
    frame = decode_telemetry("OH2F1S", _FORESAIL_UHF, 66778)
    assert frame.telemetry_id == "UHF_HK"
    f = _by_name(frame)
    assert f["identity"].unit == "OH2F1S"
    assert f["frame_sequence"].raw_value == 0x85A7
    assert f["service_type"].raw_value == 3 and f["service_subtype"].raw_value == 4
    assert f["timestamp"].unit == "2026-10-18 01:31:50 UTC"
    assert f["uptime"].raw_value == 26874
    assert f["total_tx_frames"].raw_value == 2047
    assert f["hardware_side_a_bootcount"].raw_value == 107
    assert f["hardware_side_b_bootcount"].raw_value == 82
    assert f["symbol_rate_rx"].raw_value == 1 and f["symbol_rate_tx"].raw_value == 1
    assert f["my_window_length"].raw_value == 250 and f["peer_window_length"].raw_value == 250
    assert f["mcu_temperature"].scaled_value == pytest.approx(1.1)
    assert f["background_rssi"].raw_value == -55


def test_foresail1p_adcs_housekeeping_real_frame() -> None:
    frame = decode_telemetry("OH2F1S", _FORESAIL_ADCS, 66778)
    assert frame.telemetry_id == "ADCS_HK"
    f = _by_name(frame)
    assert f["mjd"].raw_value == pytest.approx(61331.0625)
    # The ADCS was off: the compressed quaternion is the identity (0, 0, 0, 1) with the
    # omitted component at index 3.
    for name in ("q1", "q2", "q3"):
        assert abs(f[f"attitude_quaternion_{name}"].scaled_value) < 1e-3
    assert f["attitude_quaternion_index"].raw_value == 3
    assert f["attitude_quaternion_largest"].scaled_value == pytest.approx(1.0, abs=1e-3)
    assert f["magnetometer_measurement_y"].raw_value == 6141
    assert f["attitude_variance"].scaled_value == pytest.approx(0.5)


def test_foresail1p_unknown_service_stays_raw() -> None:
    frame = decode_telemetry("OH2F1S", _FORESAIL_UHF[:23] + b"\x09\x09" + _FORESAIL_UHF[25:], 66778)
    assert frame.telemetry_id is None and not frame.fields


def test_foresail1p_repeater_frame_is_matched_by_the_virtual_channel_bits() -> None:
    # Flags byte 0x13 = VC 3 with the frame-CRC bit; AX.25 header, then the message.
    def call(text: str) -> bytes:
        return bytes(ord(c) << 1 for c in text.ljust(6))

    frame_bytes = (
        bytes.fromhex("664f48324631531300000a")
        + bytes(5)
        + call("CQ")
        + b"\x60"
        + call("OH2XYZ")
        + b"\x61"
        + b"\x03\xf0"
        + b"HELLO"
        + bytes(6)
    )
    frame = decode_telemetry("", frame_bytes, 66778)
    assert frame.telemetry_id == "REPEATER"
    f = _by_name(frame)
    assert f["dest_callsign"].unit == "CQ"
    assert f["src_callsign"].unit == "OH2XYZ"
    assert f["payload"].unit == "HELLO"


def test_foresail1p_event_info_runs_to_the_end_less_the_tag() -> None:
    frame_bytes = (
        bytes.fromhex("664f48324631530985a6054400fa008c0b340b3400071004016ad42186")
        + bytes.fromhex("1234")
        + b"AB"
        + bytes.fromhex("deadbeef")
    )
    frame = decode_telemetry("OH2F1S", frame_bytes, 66778)
    assert frame.telemetry_id == "EVENT_1"
    f = _by_name(frame)
    assert f["rid"].raw_value == 0x3412
    assert f["info"].unit == "QUI="  # base64 of b"AB"


def test_payload_match_mask_compares_only_the_masked_bits() -> None:
    from comms.telemetry.decoder import _payload_matches

    match = {"offset": 1, "hex": "03", "mask": "03"}
    assert _payload_matches(match, bytes([0, 0x13]))
    assert _payload_matches(match, bytes([0, 0xFF]))
    assert not _payload_matches(match, bytes([0, 0x11]))
    assert not _payload_matches(match, bytes([0]))  # too short


# -- CroCube (NORAD 62394): comma-separated ASCII telemetry (SatNOGS crocube.ksy) --
# The frame bodies follow the ksy's layout; no real CroCube telemetry frame is recorded yet.


def test_crocube_defs_list_every_message_type() -> None:
    defs = get_telemetry_id_defs(62394)
    assert defs is not None
    assert set(defs) == {"MGS", "OBC", "PSU", "SOL", "ATR", "U", "V", "MSG", "BEACON"}


def test_crocube_obc_has_the_battery_field_lasarsat_lacks() -> None:
    tf = decode_telemetry("9A0CC", b"OBC,3,120,5000,8100,25,4096", norad=62394)
    assert tf.telemetry_id == "OBC"
    by_name = {f.name: f.raw_value for f in tf.fields}
    assert by_name == {
        "obc_reset_cnt": 3,
        "obc_uptime": 120,
        "obc_uptime_tot": 5000,
        "obc_bat": 8100,
        "obc_temp_mcu": 25,
        "obc_freemem": 4096,
    }


def test_crocube_mgs_has_sixteen_fields_and_atr_nine() -> None:
    mgs = decode_telemetry("9A0CC", ("MGS," + ",".join(str(i) for i in range(16))).encode(), 62394)
    names = [f.name for f in mgs.fields]
    assert len(names) == 16 and names[-1] == "mgs_ext_gyr_z"
    assert mgs.fields[-1].raw_value == 15
    atr = decode_telemetry("9A0CC", b"ATR,1,0,2,3,4,5,6,7,99", 62394)
    assert [f.raw_value for f in atr.fields] == [1, 0, 2, 3, 4, 5, 6, 7, 99]


def test_crocube_vhf_and_uhf_beacons_use_their_own_names() -> None:
    body = b",100,2000,3,4,25,26,27,8,9A0CC,50,60,70,80"
    v = decode_telemetry("9A0CC", b"V" + body, 62394)
    u = decode_telemetry("9A0CC", b"U" + body, 62394)
    assert v.telemetry_id == "V" and u.telemetry_id == "U"
    assert len(v.fields) == len(u.fields) == 13
    assert v.fields[0].name == "vhf_uptime" and u.fields[0].name == "uhf_uptime"
    assert {f.name: f for f in v.fields}["vhf_last_digipeater"].unit == "9A0CC"


def test_crocube_psu_channel_bits_and_free_text() -> None:
    psu = decode_telemetry("9A0CC", b"PSU,1,2,3,8000,10,11,12,13,5,1,7", norad=62394)
    by_name = {f.name: f.raw_value for f in psu.fields}
    assert by_name["psu_ch0_state"] == 1 and by_name["psu_ch1_state"] == 0
    assert by_name["psu_gnd_wdt"] == 7
    text = decode_telemetry("9A0CC", b"Hello CroCube", norad=62394)
    assert text.telemetry_id == "MSG"


# -- CANVAS (NORAD 68635): CCSDS space packets in AX.25 (SatNOGS canvas.ksy) --


def test_canvas_beacon_is_decoded_by_its_apid() -> None:
    payload = bytearray(248)
    payload[0:6] = bytes([0x08, 0x20, 0xC0, 0x05, 0x00, 0xF1])  # APID 0x20, sequence count 5
    payload[6:10] = (845916710).to_bytes(4, "big")
    payload[12:16] = bytes([1, 2, 3, 4])  # software version and image id
    payload[28:32] = (1234).to_bytes(4, "big")  # MET seconds
    payload[128:130] = (-7).to_bytes(2, "big", signed=True)  # battery current
    payload[155] = 0b00000010  # payload alive state (low two bits)
    payload[156] = 0b00010000  # UHF deploy
    payload[157] = 0b10000001  # boom counter, UHF power
    payload[244:248] = (0xDEADBEEF).to_bytes(4, "big")
    tf = decode_telemetry("CANVAS", bytes(payload), norad=68635)
    assert tf.telemetry_id == "BEACON"
    by_name = {f.name: f.raw_value for f in tf.fields}
    assert by_name["application_process_id"] == 0x20
    assert by_name["sequence_count"] == 5
    assert by_name["time_stamp_seconds"] == 845916710
    assert (by_name["bcn_fsw_major_version"], by_name["bcn_fsw_image_id"]) == (1, 4)
    assert by_name["bcn_des_met_time_sec"] == 1234
    assert by_name["bcn_battery_curr"] == -7
    assert by_name["bcn_pld_alive_state"] == 2
    assert by_name["bcn_eps_uhf_dep"] == 1 and by_name["bcn_eps_boom_cnt"] == 1
    assert by_name["bcn_eps_uhf_pwr"] == 1 and by_name["bcn_eps_sap1_dep"] == 0
    assert by_name["checksum"] == 0xDEADBEEF


def test_canvas_other_packet_shows_only_its_header() -> None:
    # The first bytes of a frame received from the real satellite (2026-10-21, APID 5).
    payload = bytes.fromhex("0805c09f00cd326baa2600050000005e")
    tf = decode_telemetry("LASP", payload, norad=68635)
    assert tf.telemetry_id == "CCSDS"
    by_name = {f.name: f for f in tf.fields}
    assert by_name["application_process_id"].raw_value == 5
    assert by_name["sequence_count"].raw_value == 159
    assert by_name["time_stamp_seconds"].raw_value == 845916710
    assert by_name["packet_body"].unit == "00 00 00 5e"


# -- KOSTKA (69935, no message identifier: told apart by field count) and GRBBeta (60237) --
# Beacons copied from JJ1WTK's satellite log (2026-10).


def test_kostka_obc_is_told_apart_by_its_nine_numeric_fields() -> None:
    tf = decode_telemetry("OK0KST", b"255,0,11,8660379,9117082,1792507744,0,22.20,12.39", 69935)
    assert tf.telemetry_id == "OBC"
    by_name = {f.name: f for f in tf.fields}
    assert by_name["obc_cpu_load_perc"].scaled_value == 11
    assert by_name["obc_temp_mcu"].scaled_value == pytest.approx(22.20)
    assert by_name["obc_temp_ext"].unit == "°C"


def test_kostka_mrg_reports_the_last_digipeated_station() -> None:
    tf = decode_telemetry(
        "OK0KST", b"U,9008906,9008995,1,0,1632,1537,1475,6,JA1HCB,8048,980794,98,120", 69935
    )
    assert tf.telemetry_id == "U"
    by_name = {f.name: f for f in tf.fields}
    assert by_name["mrg_digipeater_cnt"].scaled_value == 6
    assert by_name["mrg_last_digipeater"].unit == "JA1HCB"
    assert by_name["mrg_tx_packet_cnt"].scaled_value == 980794


def test_kostka_sol_and_eps_are_told_apart_by_field_count() -> None:
    sol = decode_telemetry(
        "OK0KST", b"-5.12,-255.00,-12.50,-9.31,-23.06,3.35,3.35,3.35,3.35,3.35", 69935
    )
    assert sol.telemetry_id == "SOL"
    assert {f.name: f for f in sol.fields}["sol_temp_xp"].scaled_value == pytest.approx(-5.12)
    eps = decode_telemetry(
        "OK0KST", b"9043136,9043238,1,36669,36376,6,16434,269,0,0,68,89,15,50,84", 69935
    )
    assert eps.telemetry_id == "EPS"
    assert len(eps.fields) == 15


def test_kostka_free_text_is_not_mistaken_for_numeric_telemetry() -> None:
    tf = decode_telemetry("OK0KST", b"1,2,3,4,5,6,7,8,hello", 69935)  # nine fields, but not numbers
    assert tf.telemetry_id == "MSG"
    assert decode_telemetry("OK0KST", b"hello world", 69935).telemetry_id == "MSG"


def test_grbbeta_uhf_beacon_uses_the_u_layout() -> None:
    tf = decode_telemetry(
        "HA2GRB",
        b"U,6860489,71980801,68,2,2011,1976,1868,806,DL7NDR,30675,5635053,103,125\x00",
        60237,
    )
    assert tf.telemetry_id == "U"
    by_name = {f.name: f for f in tf.fields}
    assert by_name["uhf_digipeater_cnt"].scaled_value == 806
    assert by_name["uhf_last_digipeater"].unit == "DL7NDR"


# -- GRBBeta binary PSU housekeeping (CSP) and the CW beacons of KOSTKA / MARINA / GRBBeta --
# These follow the SatNOGS ksy files; no real frame has been checked yet.


def _grbbeta_psu_frame() -> bytes:
    """A 75-byte CSP frame (source 2, source port 9) with known values, per psu_hk_t."""
    import struct

    header = (2 << 30) | (2 << 25) | (1 << 20) | (10 << 14) | (9 << 8) | 0x01
    body = struct.pack(
        "<IIIIHII" + "H" * 9 + "BBB" + "H" * 7 + "HHHHH",
        1_792_000_000, 1000, 2000, 3, 4, 5, 6,
        3300, 3301, 3302, 100, 101, 102, 5, 6, 7,
        0b101, 0b0100101, 2,
        11, 12, 13, 14, 15, 16, 17,
        8100, 20, 30, 293, 295,
    )  # fmt: skip
    return struct.pack(">I", header) + body


def test_grbbeta_binary_psu_frame_is_decoded_by_its_csp_header() -> None:
    payload = _grbbeta_psu_frame()
    assert len(payload) == 75
    tf = decode_telemetry("HA2GRB", payload, 60237)
    assert tf.telemetry_id == "PSU"
    by_name = {f.name: f for f in tf.fields}
    assert by_name["csp_source"].scaled_value == 2
    assert by_name["csp_src_port"].scaled_value == 9
    assert by_name["csp_dst_port"].scaled_value == 10
    assert by_name["csp_crc"].scaled_value == 1
    assert by_name["psu_utc_time_stamp_rtc"].unit.endswith("UTC")
    assert by_name["psu_uptime_tot"].scaled_value == 2000
    assert by_name["psu_pv_in_volt3"].scaled_value == 3302
    assert by_name["psu_pv_in_power1"].scaled_value == 5
    assert [by_name[f"psu_mppt_ch{i}_state"].scaled_value for i in (1, 2, 3)] == [1, 0, 1]
    assert by_name["psu_channel_0_status"].scaled_value == 1
    assert by_name["psu_channel_1_status"].scaled_value == 0
    assert by_name["psu_channel_5_status"].scaled_value == 1
    assert by_name["psu_system_state"].scaled_value == 2
    assert by_name["psu_out_sys_bat_unreg_amp"].scaled_value == 17
    assert by_name["psu_bat_volt"].scaled_value == 8100
    assert by_name["psu_system_temp_kelvin"].scaled_value == 295
    assert len(tf.fields) == 50


def test_grbbeta_ascii_beacons_still_decode_next_to_the_binary_frame_type() -> None:
    tf = decode_telemetry("HA2GRB", b"V,100,200,3,4,5,6,7,8,JA1HCB,10,11,12,13", 60237)
    assert tf.telemetry_id == "V"
    assert {f.name: f for f in tf.fields}["vhf_last_digipeater"].unit == "JA1HCB"


def test_grbbeta_psu_tab_is_offered_next_to_the_ascii_ones() -> None:
    defs = get_telemetry_id_defs(60237)
    assert defs is not None
    assert {"U", "V", "PSU", "MSG", "BEACON"} <= set(defs)


def test_cw_beacons_of_kostka_marina_and_grbbeta_decode() -> None:
    from comms.telemetry.cw_frames import decode_cw_frame

    for norad, text in (
        (69935, "DEOK0KST=U1234R2T295P301AR"),
        (69920, "DEOM9MAR=U1234R2T295P301AR"),
    ):
        result = decode_cw_frame(norad, text)
        assert result is not None
        by_name = {f.name: f for f in result.fields}
        assert by_name["uptime_total"].scaled_value == 1234
        assert by_name["reset_number"].scaled_value == 2
        assert by_name["temp_pa"].scaled_value == 301
    grb = decode_cw_frame(60237, "DEHA2GRB=U10R2T295P301")  # no "ar" needed; uptime is in minutes
    assert grb is not None
    assert {f.name: f for f in grb.fields}["uptime_total"].scaled_value == 600
