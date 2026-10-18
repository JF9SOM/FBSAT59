"""Tests for HADES-SA's telemetry in the HADES FSK mode (800 baud 2FSK, AMSAT-EA Genesis)."""

from __future__ import annotations

import numpy as np
import pytest

from comms import mode_detection
from comms.telemetry import hades_fields as hf
from comms.telemetry.hades_fsk import HADES_SA_LENGTHS, build_packet, packet_lengths, parse_packet

# Real payloads (descrambled, no CRC) from SatNOGS Network, HADES-SA, 2026-10-17/18.
POWER = bytes.fromhex("13ae8b0901434f386560000bb16c10d3e3533d0fd201ac6d3000000000")
TEMPERATURE = bytes.fromhex("2312f60801ff636663ff6e706dff78")
STATUS = bytes.fromhex(
    "33868b09018 48b09010100444700350 01bff00008400ffffffffff00643800122500004578004 3".replace(
        " ", ""
    )
)
BBS = bytes([0xF3]) + b"-" * 30 + b"No data" * 5 + bytes(5)


def test_matcher_picks_the_800_baud_genesis_downlink_of_hades_sa() -> None:
    sa = {"norad_cat_id": 68446, "mode": "GENESIS FSK", "baud": 800}
    assert mode_detection.is_hades_fsk_transmitter(sa)
    assert not mode_detection.is_hades_fsk_transmitter({**sa, "baud": 200})
    assert not mode_detection.is_hades_fsk_transmitter({**sa, "mode": "SSDV"})
    # UNNE-1B keeps its own rule
    unne = {"norad_cat_id": 98205, "mode": "FSK", "baud": 200}
    assert mode_detection.is_hades_fsk_transmitter(unne)
    assert 68446 in mode_detection.HADES_FSK_NORADS


def test_length_table_of_hades_sa_differs_for_status_and_bbs() -> None:
    assert packet_lengths(3) is HADES_SA_LENGTHS
    assert packet_lengths(3)[3] == 41 and packet_lengths(3)[15] == 73
    assert packet_lengths(12)[3] == 29  # UNNE-1B: unchanged
    assert 10 not in packet_lengths(3) and 11 not in packet_lengths(3)


def test_a_hades_sa_status_packet_round_trips_through_the_framing() -> None:
    raw = build_packet(STATUS)
    assert len(raw) == 41
    payload, repaired = parse_packet(raw, 3) or (b"", True)
    assert payload == STATUS and not repaired


def _values(payload: bytes) -> dict[str, object]:
    decoded = hf.decode_payload(payload)
    assert decoded is not None
    return {f.name: f.value for f in decoded[1]}


def test_status_fields_match_the_real_data() -> None:
    v = _values(STATUS)
    assert v["npayload"] == 68 and v["nwire"] == 71
    assert v["payload_frames"] == 120 and v["current_image_id"] == 67
    assert v["bate"] == 0 and v["npayloadfails"] == 3 and v["lstrst"] == 5
    assert "17,402,756 s" in str(v["uptime"])
    assert v["rx_percentage"] == 100 and v["vbat_higher_than_vbus"] == 69


def test_power_and_temperature_use_the_family_layout() -> None:
    v = _values(POWER)
    assert v["vbat1"] == pytest.approx(3964.8)
    assert v["ibat"] == -84  # charging
    t = _values(TEMPERATURE)
    assert t["tpb"] == 9.5 and t["tpa"] is None  # panel A: 255, not used


def test_empty_bbs_packet_is_decoded() -> None:
    v = _values(BBS)
    assert v["callsign0"] == "------" and v["message4"] == "No data"
    assert v["codec2frames3"] == 0
    frame = hf.telemetry_frame(BBS, 68446, "HADES-SA")
    assert frame is not None and frame.telemetry_id == "15" and frame.telemetry_label == "BBS data"


def test_a_filled_bbs_entry_shows_its_text() -> None:
    data = bytearray(BBS)
    data[1:7] = b"EA4ABC"  # callsign 0
    data[31:38] = b"Hola 73"  # message 0
    data[66] = 12  # CODEC2 frames of entry 0
    v = _values(bytes(data))
    assert (v["callsign0"], v["message0"], v["codec2frames0"]) == ("EA4ABC", "Hola 73", 12)


def test_decoded_tabs_of_hades_sa_have_no_ssdv_or_unne_voice() -> None:
    sa = hf.type_tabs(3)
    assert "3" in sa and "15" in sa and "10" not in sa and "6" not in sa
    assert sa["15"]["label"] == "15  BBS data"
    unne = hf.type_tabs()
    assert unne["15"]["label"] == "15  Voice"


def test_unne1b_payloads_still_use_the_family_tables() -> None:
    unne_status = bytes([0x3C]) + bytes(26)  # address 12
    decoded = hf.decode_payload(unne_status)
    assert decoded is not None and decoded[0].length == 27


# ---- the demodulator, on synthetic I/Q -------------------------------------------------- #

FS = 250_000


def _fsk(frames: list[bytes], gap_s: float = 2.0, snr_db: float = 22.0) -> np.ndarray:
    rng = np.random.default_rng(4)
    bits: list[int] = [1, 0] * 400
    for body in frames:
        raw = b"\xaa" * 16 + b"\xbf\x35" + bytes([len(body)]) + body
        bits += [(b >> (7 - i)) & 1 for b in raw for i in range(8)]
        bits += [1, 0] * int(gap_s * 800 / 2)
    sym = np.array(bits)
    n = int(len(sym) * FS / 800)
    idx = (np.arange(n) * 800 // FS).clip(max=len(sym) - 1)
    freq = np.where(sym[idx] == 1, -650.0, 950.0) + 150.0
    sig = np.exp(2j * np.pi * np.cumsum(freq) / FS)
    sigma = 10 ** (-snr_db / 20) / np.sqrt(2)
    noise = sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    return (sig + noise).astype(np.complex64)


def test_stream_decodes_hades_sa_telemetry_from_iq() -> None:
    pytest.importorskip("scipy")
    from comms.telemetry.hades_sa_fsk import HadesSaFskStream

    bodies = [build_packet(STATUS), build_packet(POWER), build_packet(BBS)]
    iq = np.concatenate([_fsk(bodies), np.zeros(8 * FS, dtype=np.complex64)])
    stream = HadesSaFskStream(FS)
    got = []
    for a in range(0, len(iq), FS // 4):
        got += stream.feed(iq[a : a + FS // 4])
    got += stream.flush()
    assert [f.payload for f in got] == [STATUS, POWER, BBS]
    assert all(f.address == 3 and not f.repaired for f in got)
    assert [f.type_name for f in got] == ["Status", "Power", "BBS data"]
    assert got[0].time_s < got[1].time_s < got[2].time_s
    assert abs(got[0].carrier_hz - 300.0) < 60.0  # centre of the tones (-500 / +1100 Hz)


def test_stream_ignores_ssdv_voice_and_noise() -> None:
    pytest.importorskip("scipy")
    from comms.telemetry.hades_sa_fsk import HadesSaFskStream

    ssdv_like = bytes([0xA3]) + bytes(250)  # size 251, type 10
    iq = np.concatenate([_fsk([ssdv_like]), np.zeros(8 * FS, dtype=np.complex64)])
    stream = HadesSaFskStream(FS)
    assert stream.feed(iq) + stream.flush() == []
    rng = np.random.default_rng(1)
    noise = (rng.standard_normal(12 * FS) + 1j * rng.standard_normal(12 * FS)).astype(np.complex64)
    assert HadesSaFskStream(FS).feed(noise) == []
