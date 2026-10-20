"""HADES-L (NORAD 98206): 800 baud 2FSK bursts in the Telemetry tab's HADES FSK mode."""

from __future__ import annotations

import numpy as np

from comms import mode_detection
from comms.telemetry import hades_fields as hf
from comms.telemetry import hades_fsk
from comms.telemetry.hades_fsk_sdr import make_stream
from comms.telemetry.hades_l_fsk import (
    HADES_L_ADDRESS,
    HadesLFskStream,
    _repair,
    _verified,
)

# A real power-statistics packet (type 4, address 5), descrambled, no CRC: HADES-L, decoded from
# an I/Q recording of 2026-10-20 12:57:45 UTC (the CRC was valid as received).
POWER_STATS = bytes.fromhex(
    "45d73517009b80701ff03f3f3f6d0800fcc5687a704142419 10c0002220000147e".replace(" ", "")
)
RATE = 250_000.0
BAUD = 800.0


def _fsk(bits: str, carrier_hz: float, amplitude: float) -> np.ndarray:
    """Continuous-phase 2FSK at 800 baud, tones 1.6 kHz apart (bit 1 = the higher tone)."""
    sps = RATE / BAUD
    n = int(round(len(bits) * sps))
    index = np.minimum((np.arange(n) / sps).astype(int), len(bits) - 1)
    sign = np.array([1.0 if b == "1" else -1.0 for b in bits])[index]
    phase = 2 * np.pi * np.cumsum(carrier_hz + sign * 800.0) / RATE
    return (amplitude * np.exp(1j * phase)).astype(np.complex64)


def _burst_bits(payload: bytes, size_byte: int | None = None, training: int = 240) -> str:
    packet = hades_fsk.build_packet(payload)
    size = len(packet) if size_byte is None else size_byte
    return (
        "10" * (training // 2)
        + hades_fsk.SYNC_BITS
        + f"{size:08b}"
        + "".join(f"{b:08b}" for b in packet)
    )


def _recording(
    bursts: list[tuple[str, float]], amplitude: float = 0.5, seed: int = 1, gap_s: float = 20.0
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    total = int(RATE * (3 + gap_s * len(bursts) + 6))
    iq = (rng.standard_normal(total) + 1j * rng.standard_normal(total)).astype(np.complex64)
    iq *= np.float32(1 / np.sqrt(2))
    for i, (bits, carrier) in enumerate(bursts):
        burst = _fsk(bits, carrier, amplitude)
        start = int((3 + gap_s * i) * RATE)
        iq[start : start + len(burst)] += burst
    return iq


def _decode(iq: np.ndarray, chunk: int = 20_000) -> list[hades_fsk.HadesFrame]:
    stream = HadesLFskStream(RATE)
    frames: list[hades_fsk.HadesFrame] = []
    for i in range(0, len(iq), chunk):
        frames += stream.feed(iq[i : i + chunk])
    return frames + stream.flush()


def test_matcher_selects_the_800_baud_fsk_transmitter_of_hades_l() -> None:
    hades_l = {"norad_cat_id": 98206, "mode": "FSK", "baud": 800}
    assert mode_detection.is_hades_fsk_transmitter(hades_l)
    assert not mode_detection.is_hades_fsk_transmitter({**hades_l, "baud": 200})
    assert not mode_detection.is_hades_fsk_transmitter({**hades_l, "mode": "FM"})
    assert 98206 in mode_detection.HADES_FSK_NORADS


def test_make_stream_picks_the_hades_l_demodulator() -> None:
    assert isinstance(make_stream(RATE, 98206), HadesLFskStream)
    assert not isinstance(make_stream(RATE, 98205), HadesLFskStream)
    assert not isinstance(make_stream(RATE, 68446), HadesLFskStream)


def test_real_packet_is_verified_and_its_fields_decode() -> None:
    raw = hades_fsk.build_packet(POWER_STATS)
    assert len(raw) == 35
    data = bytes([35]) + raw  # the size byte comes first
    assert _verified(data, HADES_L_ADDRESS) == POWER_STATS
    assert _verified(data, 3) is None  # another satellite's address
    decoded = hf.decode_payload(POWER_STATS)
    assert decoded is not None and decoded[0].name == "Power stats"
    values = {f.name: f.value for f in decoded[1]}
    assert "17 d 14:31:51" in str(values["sclock"])
    assert 3900 < float(str(values["minvbat1"])) < 4100


def test_a_burst_is_decoded_and_verified() -> None:
    iq = _recording([(_burst_bits(POWER_STATS), -4600.0)])
    frames = _decode(iq)
    assert [f.payload for f in frames] == [POWER_STATS]
    frame = frames[0]
    assert not frame.repaired and frame.address == 5 and frame.packet_type == 4
    assert abs(frame.carrier_hz - -4600.0) < 150.0  # the middle of the two tones
    # The burst starts 3 s in and its 240 training bits last 0.3 s: the frame is placed at the sync.
    assert 3.2 < frame.time_s < 3.4


def test_two_bursts_with_different_carriers_and_a_corrupt_size_byte() -> None:
    # The size byte is not covered by the CRC: a wrong one must not cost the packet.
    iq = _recording(
        [
            (_burst_bits(POWER_STATS, size_byte=3), -9000.0),
            (_burst_bits(POWER_STATS), 6500.0),
        ]
    )
    frames = _decode(iq)
    assert [f.payload for f in frames] == [POWER_STATS, POWER_STATS]
    assert frames[1].carrier_hz > 5000 > -5000 > frames[0].carrier_hz


def test_a_packet_for_another_address_is_not_reported() -> None:
    other = bytes([(POWER_STATS[0] & 0xF0) | 3]) + POWER_STATS[1:]  # HADES-SA's address
    assert _decode(_recording([(_burst_bits(other), -4000.0)])) == []


def test_noise_alone_gives_no_frame() -> None:
    rng = np.random.default_rng(5)
    n = int(RATE * 25)
    noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
    assert _decode(noise) == []


def test_repair_flips_only_the_least_reliable_bits() -> None:
    packet = hades_fsk.build_packet(POWER_STATS)
    bits = np.unpackbits(np.frombuffer(bytes([35]) + packet, dtype=np.uint8)).astype(np.int8)
    reliability = np.full(len(bits), 0.9)
    broken = bits.copy()
    for position in (40, 120):  # two received bits that are wrong *and* unreliable
        broken[position] ^= 1
        reliability[position] = 0.01
    repaired = _repair(broken, reliability, HADES_L_ADDRESS)
    assert repaired == (POWER_STATS, 2)
    # An error among the reliable bits is not within reach: nothing is invented.
    broken[200] ^= 1
    assert _repair(broken, reliability, HADES_L_ADDRESS) is None
    # Four wrong bits are more than the three that may be flipped.
    four = bits.copy()
    reliability = np.full(len(bits), 0.9)
    for position in (20, 60, 100, 140):
        four[position] ^= 1
        reliability[position] = 0.01
    assert _repair(four, reliability, HADES_L_ADDRESS) is None
