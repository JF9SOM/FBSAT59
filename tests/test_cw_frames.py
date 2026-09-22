"""Tests for comms/telemetry/cw_frames.py -- ARICA-2 bit-packed CW housekeeping.

The two real frames below were received on 2026-09-20 (IQ recording of the
pass, decoded by the CW Decoder tab) and checked field by field against the
operator's published HK1/HK3 tables, so they pin the whole chain: bit layout,
flag polarities, the conversions, and the angular-velocity range table.
"""

from __future__ import annotations

import pytest

from comms.telemetry.cw_frames import (
    _angvel_text,
    build_satnogs_frame,
    decode_cw_frame,
    is_near_miss,
    load_cw_frames,
    match_frame_key,
    normalize_block,
)
from comms.telemetry.decoder import get_telemetry_id_defs

ARICA2 = 68796
HK1_REAL = "2FFE8594EB880124"
# The Morse decoder printed a wrong first digit ('1'); the repeated '0D7C2A' and the
# battery-board temperature (raw 1 -> -237.576, the operator's constant) fix it.
HK3_REAL = "00D7C2A8D6B8EA"


def _shown(hex_text: str) -> dict[str, str]:
    result = decode_cw_frame(ARICA2, hex_text)
    assert result is not None
    return {f.label: (f.unit if f.is_string else f"{f.scaled_value:.3f}") for f in result.fields}


def _bits_to_hex(bits: str) -> str:
    return f"{int(bits, 2):0{len(bits) // 4}X}"


def _hk2(hour: int, minute: int, second: int, lat: int, lon: int, north: int, east: int) -> str:
    """Build an HK2 frame: 4 unused, 3 flags, h5 m6 s6, lat sign+7, lon sign+8, alt 7."""
    bits = (
        "0000"
        + "0"
        + "1"
        + "1"
        + f"{hour:05b}{minute:06b}{second:06b}"
        + f"{north}{lat:07b}"
        + f"{east}{lon:08b}"
        + "0000000"
    )
    return _bits_to_hex(bits)


class TestHk1:
    def test_is_recognised_and_valid(self) -> None:
        result = decode_cw_frame(ARICA2, HK1_REAL)
        assert result is not None
        assert result.key == "HK1"
        assert result.valid

    def test_fields_match_the_operators_table(self) -> None:
        shown = _shown(HK1_REAL)
        assert shown["Command ID"] == "2.000"
        assert shown["Setup Mode"] == "Complete"
        assert shown["Antenna Deployment"] == "Complete"
        assert shown["SBD error"] == "Abnormal"
        assert shown["EPS error"] == "Normal"
        assert shown["Status of Attitude Control"] == "the attitude control is NOT possible"
        assert shown["Count of mode change to power-saving mode"] == "2.000"
        assert shown["Reboot count of SPRESENSE 1"] == "4.000"
        assert shown["Reboot count of SPRESENSE 2"] == "7.000"
        assert shown["Communication to another SPRESENSE"] == "SPR1sub5→SPR2sub1"
        assert shown["Count of Attitude Control"] == "4.000"

    def test_angular_velocity_shows_the_operators_ranges(self) -> None:
        shown = _shown(HK1_REAL)
        assert shown["Angular Velocity X"] == "-2.591 ≦ Gx ＜ -2.061"
        assert shown["Angular Velocity Y"] == "0.377 ≦ Gy ＜ 0.615"
        assert shown["Angular Velocity Z"] == "0 ≦ Gz ＜ 0.173"


class TestHk3:
    def test_is_recognised_and_valid(self) -> None:
        result = decode_cw_frame(ARICA2, HK3_REAL)
        assert result is not None
        assert result.key == "HK3"
        assert result.valid

    def test_conversions_match_the_operators_table(self) -> None:
        shown = _shown(HK3_REAL)
        assert shown["Battery Daughter Board 1 Temp"] == "-237.576"
        assert shown["UHF communication device Temp"] == "36.682"
        assert shown["Voltage of the received radio"] == "1.059"
        assert shown["Battery Voltage"] == "8.201"
        assert shown["Count of Uplink (UHF)"] == "13.000"
        assert shown["Last Command ID (SBD)"] == "1.000"

    def test_flag_texts(self) -> None:
        shown = _shown(HK3_REAL)
        assert shown["Battery Heater"] == "off"  # bit 1: the operator's table says off
        assert shown["Power Flow Status"] == "charge"
        assert shown["ICM-20948①"] == "off"
        assert shown["ICM-20948②"] == "on"
        assert shown["nichrome wire cutter"] == "off"
        assert shown["gamma-ray detector"] == "on"


class TestHk2:
    def test_gps_frame_decodes(self) -> None:
        shown = _shown(_hk2(7, 3, 11, 78, 168, north=1, east=1))
        assert shown["GPS Time"] == "07:03:11"
        assert shown["GPS Location - Latitude (+N / -S)"] == "78.000"
        assert shown["GPS Location - Longitude (+E / -W)"] == "168.000"

    def test_south_and_west_are_negative(self) -> None:
        shown = _shown(_hk2(1, 2, 3, 33, 70, north=0, east=0))
        assert shown["GPS Location - Latitude (+N / -S)"] == "-33.000"
        assert shown["GPS Location - Longitude (+E / -W)"] == "-70.000"

    @pytest.mark.parametrize(
        "args",
        [
            (24, 0, 0, 10, 10),
            (5, 60, 0, 10, 10),
            (5, 0, 60, 10, 10),
            (5, 0, 0, 91, 10),
            (5, 0, 0, 10, 181),
        ],
    )
    def test_out_of_range_values_make_the_frame_invalid(self, args: tuple[int, ...]) -> None:
        h, m, s, lat, lon = args
        result = decode_cw_frame(ARICA2, _hk2(h, m, s, lat, lon, north=1, east=1))
        assert result is not None
        assert not result.valid
        assert result.problems


class TestPlausibilityChecks:
    def test_a_set_unused_bit_makes_hk1_invalid(self) -> None:
        bits = list(f"{int(HK1_REAL, 16):064b}")
        bits[54] = "1"  # the 2-bit not_used field is bits 53-54
        corrupted = _bits_to_hex("".join(bits))
        result = decode_cw_frame(ARICA2, corrupted)
        assert result is not None
        assert not result.valid
        assert any("not_used" in p for p in result.problems)

    def test_a_set_unused_bit_makes_hk3_invalid(self) -> None:
        bits = list(f"{int(HK3_REAL, 16):056b}")
        bits[47] = "1"  # not_used = bits 45-47
        corrupted = _bits_to_hex("".join(bits))
        result = decode_cw_frame(ARICA2, corrupted)
        assert result is not None
        assert not result.valid


class TestBlockClassification:
    def test_whitespace_and_case_are_ignored(self) -> None:
        assert normalize_block("2f fe 85\n94eb 880124") == HK1_REAL
        assert match_frame_key(ARICA2, "2f fe 85 94 eb 88 01 24") == "HK1"

    @pytest.mark.parametrize("length,key", [(16, "HK1"), (12, "HK2"), (14, "HK3")])
    def test_frame_key_follows_the_hex_length(self, length: int, key: str) -> None:
        assert match_frame_key(ARICA2, "A" * length) == key

    def test_other_lengths_and_non_hex_are_not_frames(self) -> None:
        assert match_frame_key(ARICA2, "A" * 15) is None
        assert match_frame_key(ARICA2, "2FFE8594EB88012G") is None
        assert decode_cw_frame(ARICA2, "DE JS1YSD ARICA2") is None

    def test_near_misses(self) -> None:
        assert is_near_miss(ARICA2, "2FFE8594EB88012")  # one digit dropped
        assert is_near_miss(ARICA2, "2FFE8594EB8801245")  # one digit inserted
        assert is_near_miss(ARICA2, "2FFE8594EB88012O")  # right length, one non-hex character
        assert not is_near_miss(ARICA2, "DE JS1YSD ARICA2")  # the ID text is not a frame
        assert not is_near_miss(ARICA2, "2FFE8594")

    def test_satellite_without_cw_frames(self) -> None:
        assert load_cw_frames(25544) is None
        assert decode_cw_frame(25544, HK1_REAL) is None
        assert match_frame_key(None, HK1_REAL) is None


class TestAngularVelocityText:
    EDGES = [0.0, 0.173, 0.377, 0.615, 0.895, 1.223, 1.609, 2.061, 2.591, 3.213, 3.943]

    def test_sign_magnitude_codes(self) -> None:
        assert _angvel_text(0, 5, self.EDGES, "Gz") == "0 ≦ Gz ＜ 0.173"
        assert _angvel_text(0b10000, 5, self.EDGES, "Gz") == "-0.173 ≦ Gz ＜ -0"
        assert _angvel_text(0b10111, 5, self.EDGES, "Gx") == "-2.591 ≦ Gx ＜ -2.061"
        assert _angvel_text(9, 5, self.EDGES, "Gx") == "3.213 ≦ Gx ＜ 3.943"

    def test_large_codes_only_show_a_lower_bound(self) -> None:
        assert _angvel_text(10, 5, self.EDGES, "Gx") == "|Gx| ≧ 3.943 (range not confirmed)"
        assert _angvel_text(0b11111, 5, self.EDGES, "Gx") == "|Gx| ≦ -3.943 (range not confirmed)"


class TestDecodedFieldsDefinition:
    def test_hidden_helper_fields_are_not_listed(self) -> None:
        defs = get_telemetry_id_defs(ARICA2)
        assert defs is not None
        assert set(defs) == {"HK1", "HK2", "HK3"}
        names = {f["name"] for f in defs["HK2"]["fields"]}
        assert "gps_time" in names
        assert not {"gps_hour", "gps_minute", "gps_second", "gps_lat_sign", "not_used"} & names


class TestSatnogsFrame:
    """The bytes submitted to the SatNOGS DB: 'arica-2' + beacon type + the frame
    (arica2.ksy cw1_form/cw2_form/cw3_form: 16/14/15 bytes)."""

    def test_hk1(self) -> None:
        frame = build_satnogs_frame(ARICA2, HK1_REAL)
        assert frame == b"arica-2" + b"\x01" + bytes.fromhex(HK1_REAL)
        assert frame is not None
        assert len(frame) == 0x10

    def test_hk2(self) -> None:
        text = _hk2(7, 3, 11, 78, 168, north=1, east=1)
        frame = build_satnogs_frame(ARICA2, text)
        assert frame is not None
        assert frame[:8] == b"arica-2\x02"
        assert len(frame) == 0x0E

    def test_hk3(self) -> None:
        frame = build_satnogs_frame(ARICA2, HK3_REAL)
        assert frame == b"arica-2" + b"\x03" + bytes.fromhex(HK3_REAL)
        assert frame is not None
        assert len(frame) == 0x0F

    def test_whitespace_and_case_are_ignored(self) -> None:
        assert build_satnogs_frame(ARICA2, "2f fe 85 94 eb 88 01 24") == build_satnogs_frame(
            ARICA2, HK1_REAL
        )

    def test_not_a_frame_or_no_satnogs_entry(self) -> None:
        assert build_satnogs_frame(ARICA2, "DE JS1YSD ARICA2") is None
        assert build_satnogs_frame(ARICA2, HK1_REAL[:-1]) is None
        assert build_satnogs_frame(25544, HK1_REAL) is None


ORIGAMISAT2 = 68795
# Two independent CW copies of the same 28-byte frame, received 2026-09-22 (CW Decoder
# tab, callsign JS1YRU). The two transmissions are ~73s apart (satellite_time confirms
# it), so bytes that differ between them (battery_current, angular velocity) are
# genuinely dynamic, not a copy error; the ones that agree pin the byte layout --
# most tellingly the four UVC threshold bytes, which land on exactly the example
# voltages (7.5/6.6/7.2/6.2 V) in Figure 3 of ORI-2-0027e-OPR ver.1.1 (document_cw).
OSAT2_FRAME_A = "817F7F8E841C05827E773D160000008585230 66AB225BF4B42483E00".replace(" ", "")
OSAT2_FRAME_B = "817F7E5284 1C057082 6D3D16000000858523066AB226084B42483E00".replace(" ", "")


def _osat2_shown(hex_text: str) -> dict[str, str]:
    result = decode_cw_frame(ORIGAMISAT2, hex_text)
    assert result is not None
    return {f.label: (f.unit if f.is_string else f"{f.scaled_value:.4f}") for f in result.fields}


class TestOrigamiSat2:
    def test_both_real_frames_are_recognised_and_valid(self) -> None:
        for hex_text in (OSAT2_FRAME_A, OSAT2_FRAME_B):
            result = decode_cw_frame(ORIGAMISAT2, hex_text)
            assert result is not None
            assert result.key == "TLM"
            assert result.valid

    def test_uvc_thresholds_match_the_documents_own_example_figure(self) -> None:
        for hex_text in (OSAT2_FRAME_A, OSAT2_FRAME_B):
            shown = _osat2_shown(hex_text)
            assert shown["UVC閾値: Normal Mode復帰"] == "7.5000"
            assert shown["UVC閾値: Safe Mode移行"] == "6.6000"
            assert shown["UVC閾値: Level 1"] == "7.2000"
            assert shown["UVC閾値: Level 2"] == "6.2000"

    def test_satellite_time_advances_by_the_gap_between_receptions(self) -> None:
        shown_a = _osat2_shown(OSAT2_FRAME_A)
        shown_b = _osat2_shown(OSAT2_FRAME_B)
        t_a = float(shown_a["衛星内部時刻(UNIX秒)"])
        t_b = float(shown_b["衛星内部時刻(UNIX秒)"])
        assert t_b - t_a == pytest.approx(73.0)

    def test_mode_and_static_fields(self) -> None:
        shown = _osat2_shown(OSAT2_FRAME_A)
        assert shown["Operating Mode"] == "Normal Mode"
        assert shown["Operating Mode Status"] == "遷移完了"
        assert shown["UVC Level"] == "UVC startup successful"
        assert shown["Battery Voltage"] == "7.9375"
        assert shown["Battery Temperature"] == "4.0000"
        assert shown["ADCSモード"] == "START UP"
        assert shown["OBC起動回数"] == "35.0000"
        assert shown["予約コマンド数"] == "6.0000"
        assert shown["バス通信ヒューズカット回数"] == "0.0000"

    def test_no_satnogs_wire_format_defined_yet(self) -> None:
        """No confirmed SatNOGS submission layout exists for this satellite's CW
        frame (unlike ARICA-2's arica2.ksy *_form), so build_satnogs_frame must not
        guess one."""
        assert build_satnogs_frame(ORIGAMISAT2, OSAT2_FRAME_A) is None

    def test_hidden_helper_fields_are_not_listed(self) -> None:
        defs = get_telemetry_id_defs(ORIGAMISAT2)
        assert defs is not None
        assert "TLM" in defs
        names = {f["name"] for f in defs["TLM"]["fields"]}
        assert not {"pwrgen_reserved", "sw_no_use"} & names
