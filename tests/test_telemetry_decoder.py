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
    assert set(id_defs.keys()) == {"65", "100", "130", "TLM"}


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
    # First 3 bytes: packet length, gen timing, telemetry ID=1 (not defined
    # for OrigamiSat-2 in the current format file).
    payload = bytes([0x00, 0xFF, 0x01]) + b"\x00" * 10
    tf = decode_telemetry("JS1YRU", payload, norad=68795)
    assert tf.telemetry_id == 1
    assert not tf.has_fields


def test_raw_summary_flags_truncation_when_payload_exceeds_preview() -> None:
    # 33-byte payload (66 hex chars) — longer than the 40-char/20-byte
    # preview summary() shows, so the truncation note must appear with the
    # actual shown/total hex-char counts (not a hardcoded satellite-specific
    # number).
    payload = bytes([0x00, 0xFF, 0x01]) + b"\x00" * 30
    tf = decode_telemetry("JS1YRU", payload, norad=68795)
    assert (
        tf.summary()
        == "[raw] 00ff010000000000000000000000000000000000 (40/66 hex chars — rest omitted)"
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
