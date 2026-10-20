"""
Unit tests for comms/telemetry/decoder.py.

No network connection required; decodes fixed byte strings against the
JSON format files already checked into src/data/telemetry_formats/.
"""

from __future__ import annotations

from comms.telemetry.decoder import decode_telemetry, get_telemetry_id_defs

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
    assert set(id_defs.keys()) == {"MGS", "OBC", "PSU", "SOL", "CLS", "LOD", "U", "V"}


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
