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
    id_defs = get_telemetry_id_defs(68795)
    assert id_defs is not None
    assert set(id_defs.keys()) == {"65", "100", "130"}


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
