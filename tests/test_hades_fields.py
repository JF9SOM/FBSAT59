"""Tests for comms.telemetry.hades_fields: field decoding of real UNNE-1B packets.

The frames are real ones (descrambled payloads, CRC removed) from SatNOGS Network's
2026-10-03..16 observations of UNNE-1B and from the 2026-10-18 01:20Z IQ recording.
"""

from __future__ import annotations

from comms.telemetry import hades_fields as hf

POWER_SUN = bytes.fromhex("1ca44d0300877609625000fbb86d7f046473400ffb01d7001000000000")
POWER_DARK = bytes.fromhex("1c2e6a02000000000000007bb66d73f573733f00f60122011000000000")
TEMP = bytes.fromhex("2cbeac0200383a3838ff4440403e4a")
STATUS = bytes.fromhex("3c04b0040004b004000100540600050000010000ffffffffffff00")
POWER_STATS = bytes.fromhex("4c8c6107007bac6d03d03c3c3dd51001dbbb6d9810404040d41400271e00000102")
TEMP_STATS = bytes.fromhex("5c944e030030323030ff3630323438807e7f80ff767b7c7982")
SERIES_VBAT = bytes.fromhex(
    "eca495060002b7b8b7b7b8b8b8b8b8b7b8b8b8b8b8b8b8b8b8b7b8b7b7b7b6b7b7b7b7b7"
)
SERIES_TCPU = bytes.fromhex(
    "ec6a6a020003424244484c50545a606365696c7072747778797b7a736c68615c5752504c"
)
GAME = bytes.fromhex("ac7e48010000000300000102000201")
DEPLOY = bytes.fromhex("8c700fe0ff7404000073e0700fb4ff8724060a6d002623010101000409")
EXT_POWER = bytes.fromhex(
    "9c1800000000001800000000001400000000001400000000001800000000001800000000001c00000000"
    "001c0000000000a00f00000000a00f00000000d00f2200a501d00fd3005603d00f2200a501d00fd30056"
    "03000000000000000000000000d80fefffa3ffd80f13004d00d40f8cfbf8e7d40f74040b12"
)
EPHEMERIS_EMPTY = bytes.fromhex("cc" + "00" * 61)
# Real type 15 (voice) packet number 6 and a type 6 (sun sensors) packet from SatNOGS Network.
VOICE_PACKET = bytes.fromhex(
    "fc06063961ef0f0bed608463214a01f43be22360d3e1635d0dacd89a559a0db7645bae33db"
)
# From the 2026-10-18 recording: the type 3 frame at 01:20:22Z.
RECORDED_STATUS = bytes.fromhex("3c20fd130020fd13000100ff0a00050000010000ffffffffffff00")


def _values(payload: bytes) -> dict[str, hf.Value]:
    decoded = hf.decode_payload(payload)
    assert decoded is not None
    return {f.name: f.value for f in decoded[1]}


def test_every_type_has_unique_field_names_and_matching_skeleton() -> None:
    for number, definition in hf.TYPES.items():
        names = [f.name for f in definition.fields]
        assert len(names) == len(set(names)), number
        skeleton = hf.type_tabs()[str(number)]["fields"]
        assert [f["name"] for f in skeleton] == names  # type: ignore[index, union-attr]


def test_each_sample_packet_decodes_with_all_its_fields() -> None:
    samples = {
        1: POWER_SUN,
        2: TEMP,
        3: STATUS,
        4: POWER_STATS,
        5: TEMP_STATS,
        8: DEPLOY,
        9: EXT_POWER,
        10: GAME,
        12: EPHEMERIS_EMPTY,
        14: SERIES_VBAT,
    }
    for number, payload in samples.items():
        decoded = hf.decode_payload(payload)
        assert decoded is not None, number
        definition, fields = decoded
        assert definition.number == number
        assert len(payload) == definition.length, number
        assert [f.name for f in fields] == [f.name for f in definition.fields]


def test_power_voltages_agree_and_current_sign_follows_sun() -> None:
    sun, dark = _values(POWER_SUN), _values(POWER_DARK)
    for values in (sun, dark):
        battery = [values[k] for k in ("vbus1", "vbat1", "vbus2", "vbus3", "vbat2")]
        assert all(3900 <= v <= 4250 for v in battery)  # one battery, five ADC channels
        assert max(battery) - min(battery) < 90
        assert 2700 < values["vcpu"] < 3000
    assert sun["ibat"] < 0 < dark["ibat"]  # charging in the sun, discharging in the dark
    assert sun["spi"] > 0 and dark["spi"] == 0
    assert sun["spa"] == 0x87 * 2  # the byte counts 2 mW


def test_power_exact_values() -> None:
    v = _values(POWER_SUN)
    assert (v["spa"], v["spb"], v["spc"], v["spd"], v["spi"]) == (270, 236, 18, 196, 160)
    assert round(v["vbus1"]) == 4143 and round(v["vbat1"]) == 4120
    assert v["vbus2"] == 4112 and v["vbus3"] == 4124 and v["vbat2"] == 4076
    assert (v["ibat"], v["icpu"], v["ipl"]) == (-41, 17, 0)
    assert v["sclock"] == "216,484 s  (2 d 12:08:04)"


def test_temperatures_and_the_unused_sensor() -> None:
    v = _values(TEMP)
    assert v["tpa"] == -12.0 and v["tpb"] == -11.0 and v["tcpu"] == -3.0
    assert v["tpe"] is None  # 255: error / not used
    assert (v["teps"], v["ttx"], v["trx"]) == (-6.0, -8.0, -9.0)


def test_status_fields() -> None:
    v = _values(RECORDED_STATUS)
    assert v["uptime"] == "1,309,984 s  (15 d 03:53:04)"
    assert v["nrun"] == 1 and v["nwire"] == 10
    assert v["npayloadfails"] == 0 and v["lstrst"] == 5
    assert v["antennadeployed"] == "deployed (1)"
    assert v["mote"] == "off (0)"
    assert v["strfwd1"] == "0xFFFF" and v["strfwd3"] == 0
    assert _values(STATUS)["nwire"] == 6


def test_power_stats_min_below_max_and_signs() -> None:
    v = _values(POWER_STATS)
    # (the payload current is left out: the satellite keeps its minimum at 1 mA, above the maximum)
    for key in ("vbus1", "vbat1", "vcpu", "vbus2", "vbus3", "vbat2", "ibat", "icpu"):
        assert v["min" + key] <= v["max" + key], key
    assert round(v["minvbus1"]) == 3863 and round(v["maxvbus1"]) == 4207
    assert v["minibat"] == -213 and v["maxibat"] == 212
    assert v["minvbus2"] == 3840 and v["maxvbat2"] == 4096
    assert v["ibat_rx_charging"] == 39 and v["ibat_txhigh_discharging"] == 2


def test_temperature_stats() -> None:
    v = _values(TEMP_STATS)
    assert v["mintpa"] == -16.0 and v["maxtpa"] == 24.0
    assert v["mintpe"] is None and v["maxtpe"] is None
    assert all(v["min" + n] <= v["max" + n] for n, _l in hf._TEMPERATURES if n != "tpe")


def test_time_series_variable_decides_units_and_scale() -> None:
    battery = hf.telemetry_frame(SERIES_VBAT, 98205)
    assert battery is not None
    by_name = {f.name: f for f in battery.fields}
    assert by_name["variable"].unit == "VBAT1 (2)"
    assert by_name["s00"].unit == "4099.2 mV" and round(by_name["s00"].scaled_value) == 4099
    cpu = hf.telemetry_frame(SERIES_TCPU, 98205)
    assert cpu is not None
    samples = [f for f in cpu.fields if f.name.startswith("s") and f.name[1:].isdigit()]
    assert len(samples) == 30 and all(f.unit.endswith(" °C") for f in samples)
    assert samples[0].scaled_value == -7.0 and samples[17].scaled_value == 20.0


def test_game_deploy_extended_and_empty_ephemeris() -> None:
    game = _values(GAME)
    assert game["week_number"] == 0 and game["data0"] == 3 and game["data7"] == 1
    assert game["clock_tx"] == "84,094 s  (0 d 23:21:34)"
    deploy = _values(DEPLOY)
    assert deploy["v1oc"] == 0x0F70 and deploy["r2"] == 0x2487
    assert deploy["t0"] == 0x006D0A06 and deploy["td"] == 0x2326 and deploy["tmp"] == 9
    ext = _values(EXT_POWER)
    assert len(ext) == 60 and ext["v5"] == 0x0FD0 and ext["v4"] == 0x0FA0
    assert all(v == 0 for k, v in _values(EPHEMERIS_EMPTY).items() if k in ("utc", "lat", "cnt"))


def test_undecodable_payloads_give_none() -> None:
    assert hf.decode_payload(b"") is None
    assert hf.decode_payload(POWER_SUN[:20]) is None  # shorter than the type needs
    assert hf.decode_payload(bytes([0xFC]) + bytes(20)) is None  # a voice packet is 37 bytes
    assert hf.decode_payload(bytes([0x6C]) + bytes(100)) is None  # a sun-sensor packet is 133
    assert hf.decode_payload(bytes([0x7C]) + bytes(100)) is None  # type 7: not on UNNE-1B
    assert hf.telemetry_frame(b"\x6c", 98205) is None


def test_telemetry_frame_shapes_values_for_the_tab() -> None:
    frame = hf.telemetry_frame(POWER_SUN, 98205, "UNNE-1B")
    assert frame is not None
    assert (frame.norad, frame.telemetry_id, frame.satellite_name) == (98205, "1", "UNNE-1B")
    by_name = {f.name: f for f in frame.fields}
    assert by_name["sclock"].is_string and "2 d" in by_name["sclock"].unit
    assert by_name["peaksignal"].unit == "0.0 dB" and by_name["peaksignal"].is_string
    assert by_name["vbat1"].is_integer and by_name["vbat1"].unit == "mV"
    temp = hf.telemetry_frame(TEMP, 98205)
    assert temp is not None
    assert {f.name: f for f in temp.fields}["tpe"].unit == "n/a"


def test_packed_helper_swaps_byte_pairs_msb_first() -> None:
    # words 0xABCD and 0x1234 sent low byte first, then a lone byte 0x56
    data = bytes([0xCD, 0xAB, 0x34, 0x12, 0x56])
    assert hf._packed(data, 0, 5, 0, 12) == 0xABC
    assert hf._packed(data, 0, 5, 12, 12) == 0xD12
    assert hf._packed(data, 0, 5, 24, 12) == 0x345
    assert hf._packed(data, 0, 5, 36, 4) == 0x6


def test_voice_packet_fields() -> None:
    v = _values(VOICE_PACKET)
    assert v["number"] == 6 and abs(v["time"] - 2.4) < 1e-9  # type: ignore[operator]
    from comms.voice.hades_voice import MASK

    assert (
        v["data"]
        == bytes(a ^ b for a, b in zip(VOICE_PACKET[2:37], MASK, strict=True)).hex(" ").upper()
    )
    assert hf.telemetry_frame(VOICE_PACKET, 98205).telemetry_id == "15"  # type: ignore[union-attr]


def test_sun_sensor_words_are_high_byte_first() -> None:
    packet = bytearray([0x6C]) + bytearray(132)
    packet[1:3] = bytes([0x00, 0x80])  # td0 = 128 s
    packet[13:15] = bytes([0xFF, 0xEF])  # sensor 0, sample 0
    packet[109 + 2 * 3 : 109 + 2 * 3 + 2] = bytes([0x01, 0x02])  # sensor 3 peak
    packet[125 + 2] = 92  # sensor 2 error code
    v = _values(bytes(packet))
    assert v["td0"] == 128 and v["v0_0"] == 0xFFEF and v["p3"] == 0x0102 and v["err2"] == 92
    assert len(v) == 6 + 48 + 8 + 8
