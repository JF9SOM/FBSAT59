"""ERMINAZ-2 digipeater: TC uplink frame, audio round trip and downlink parsing.

The round trip needs scipy and reed-solomon-ccsds (skipped when they are missing).
"""

from __future__ import annotations

from math import gcd

import numpy as np
import pytest

from comms.erminaz.frames import (
    DEFAULT_RADIO_CONF,
    MAX_MESSAGE_LENGTH,
    build_digipeater_tc_frame,
    crc16_ccitt_false,
    describe_radio_conf,
    encode_callsign,
    parse_digipeater_frame,
)


def test_crc16_ccitt_false_check_value() -> None:
    assert crc16_ccitt_false(b"123456789") == 0x29B1


def test_tc_frame_layout_matches_the_ground_station_flowgraph() -> None:
    """Same bytes DL7NDR's flowgraph builds for 2U (SCID 102): header, SP header, data, CRC."""
    frame = build_digipeater_tc_frame(102, "ALL", "JF9SOM", "HELLO")
    n = len("HELLO")
    assert frame[0:3] == bytes.fromhex("206604")
    assert frame[3] == n + 28  # frame length field = total - 1
    assert len(frame) == n + 29
    assert frame[4:11] == bytes.fromhex("00c61258c00000")
    assert frame[11] == n + 14  # space packet data length - 1
    assert frame[12:14] == bytes.fromhex("a80c")
    assert frame[14:20] == b"ALL\x00\x00\x00"
    assert frame[20:26] == b"JF9SOM"
    assert frame[26 : 26 + n] == b"HELLO"
    assert frame[26 + n] == 0
    assert int.from_bytes(frame[-2:], "big") == crc16_ccitt_false(frame[:-2])


@pytest.mark.parametrize(("scid", "prefix"), [(102, "2066"), (100, "2064"), (79, "204f")])
def test_header_carries_the_spacecraft_id(scid: int, prefix: str) -> None:
    assert build_digipeater_tc_frame(scid, "ALL", "JF9SOM", "x")[0:2].hex() == prefix


def test_longest_message_fits_the_firmware_buffer() -> None:
    frame = build_digipeater_tc_frame(102, "ALL", "JF9SOM", "x" * MAX_MESSAGE_LENGTH)
    assert len(frame) <= 95  # OSDLP_MAX_TC_FRAME_SIZE
    with pytest.raises(ValueError):
        build_digipeater_tc_frame(102, "ALL", "JF9SOM", "x" * (MAX_MESSAGE_LENGTH + 1))


def test_invalid_input_is_rejected() -> None:
    with pytest.raises(ValueError):
        encode_callsign("TOOLONGCALL")
    with pytest.raises(ValueError):
        build_digipeater_tc_frame(102, "ALL", "JF9SOM", "café")
    assert encode_callsign("jf9som-5") == b"JF9SOM"


def test_radio_conf_description() -> None:
    assert describe_radio_conf(DEFAULT_RADIO_CONF) == "9600 baud FSK RS, 0 dBm, PA on"


def _tm_frame(data: bytes) -> bytes:
    packet = bytes.fromhex("0258c000") + (len(data) - 1).to_bytes(2, "big") + data
    frame = (102 << 4 | 1).to_bytes(2, "big") + bytes([1, 2]) + bytes.fromhex("1800") + packet
    return frame + b"\xe0" * (223 - len(frame))


def test_parse_digipeater_frame() -> None:
    data = bytes.fromhex("0a0c") + b"ALL\x00\x00\x00" + b"DP0PQA" + b"2026-10-26> DIGIPEATER ON\x00"
    message = parse_digipeater_frame(_tm_frame(data))
    assert message is not None
    assert (message.spacecraft_id, message.destination, message.source) == (102, "ALL", "DP0PQA")
    assert message.text == "2026-10-26> DIGIPEATER ON"
    assert message.radio_conf == 0x0A0C


def test_other_apids_are_not_digipeater_messages() -> None:
    frame = bytearray(_tm_frame(b"\x00" * 30))
    frame[6:8] = (300).to_bytes(2, "big")
    assert parse_digipeater_frame(bytes(frame)) is None


class TestAudioRoundTrip:
    """The uplink audio, FM modulated into I/Q (and as discriminator audio), is received again."""

    @pytest.fixture(autouse=True)
    def _libs(self) -> None:
        pytest.importorskip("scipy")
        pytest.importorskip("reed_solomon_ccsds")

    def test_frame_survives_encoding_scrambling_and_rs(self) -> None:
        from comms.ax100digi import rs_ccsds
        from comms.ax100digi.frame import _bits_to_bytes, _syncword_bits
        from comms.ax100digi.randomizer import ccsds_xor_sequence
        from comms.erminaz.tx import ASM, uplink_bits

        frame = build_digipeater_tc_frame(102, "ALL", "JF9SOM", "HELLO")
        bits = uplink_bits(frame, preamble_bits=64, tail_bits=0)
        sync = _syncword_bits(ASM)
        start = 64 + len(sync)
        assert np.array_equal(bits[64:start], sync)
        codeword = ccsds_xor_sequence(_bits_to_bytes(bits[start:]))
        assert rs_ccsds.decode_shortened(codeword, dual_basis=False)[1] == frame

    def test_downlink_frame_is_decoded_from_audio_and_iq(self) -> None:
        from scipy import signal

        from comms.ax100digi import rs_ccsds
        from comms.ax100digi.frame import _bytes_to_bits, _syncword_bits
        from comms.ax100digi.randomizer import ccsds_xor_sequence
        from comms.erminaz import rx
        from comms.erminaz.tx import ASM
        from comms.foresail.tx import AUDIO_RATE, gaussian_nrz_audio

        data = bytes.fromhex("0a0c") + b"ALL\x00\x00\x00DP0PQA" + b"HI FROM SPACE\x00"
        tm = _tm_frame(data)
        codeword = ccsds_xor_sequence(rs_ccsds.encode_shortened(tm, dual_basis=False))
        pre = np.tile(np.array([0, 1], dtype=np.uint8), 64)
        bits = np.concatenate([pre, _syncword_bits(ASM), _bytes_to_bits(codeword), pre[:24]])
        audio = gaussian_nrz_audio(bits.astype(np.uint8))
        silence = np.zeros(AUDIO_RATE // 2, dtype=np.float32)
        padded = np.concatenate([silence, audio, silence]).astype(np.float32)

        for inverted in (False, True):
            frames = rx.decode_iq(-padded if inverted else padded, AUDIO_RATE, audio=True)
            assert [f.payload for f in frames] == [tm]
            message = parse_digipeater_frame(frames[0].payload)
            assert message is not None and message.text == "HI FROM SPACE"

        fs = 250_000
        g = gcd(fs, AUDIO_RATE)
        up = signal.resample_poly(padded, fs // g, AUDIO_RATE // g)
        for deviation in (2400.0, 4800.0):  # modulation index 0.5 and 1.0
            carrier = np.exp(1j * 2 * np.pi * np.cumsum(up * deviation) / fs).astype(np.complex64)
            frames = rx.decode_iq(carrier, fs)
            assert [f.payload for f in frames] == [tm], deviation
