"""Tests for comms.telemetry.hades_fsk (200 baud 2FSK Genesis-family telemetry).

Numpy only; no network and no scipy. The golden packet is a real UNNE-1B frame received
on 2026-10-18 (as it came off the air, scrambled, with its CRC).
"""

from __future__ import annotations

import numpy as np

from comms.telemetry import hades_fsk as hf

# Real frame (type 1, 31 bytes with CRC) from the 2026-10-18 01:20Z UNNE-1B recording.
REAL_RAW = bytes.fromhex("1cfafbe3509a4bfdcb17f03706b3fab8a6714ea3cf7bc501b844f49e8a275b")
REAL_PAYLOAD = bytes.fromhex("1c7afd1300ac25854d5f00cbb86d86007423400ffb01c7011000000000")
# Other real, verified payloads of the same pass (uptime counter steps by 30 s).
REAL_PAYLOADS = [
    bytes.fromhex("3c20fd130020fd13000100ff0a00050000010000ffffffffffff00"),  # type 3
    bytes.fromhex("ac5cfd130002000300000102000201"),  # type 10
    REAL_PAYLOAD,  # type 1
]


def _fsk_burst(bits: str, rate: float, carrier_hz: float, amplitude: float) -> np.ndarray:
    """Continuous-phase 2FSK (bit 1 = higher tone, shift 1.7 kHz) at ``carrier_hz``."""
    sps = rate / hf.BAUD
    n = int(round(len(bits) * sps))
    index = np.minimum((np.arange(n) / sps).astype(int), len(bits) - 1)
    sign = np.array([1.0 if b == "1" else -1.0 for b in bits])[index]
    phase = 2 * np.pi * np.cumsum(carrier_hz + sign * 850.0) / rate
    burst: np.ndarray = (amplitude * np.exp(1j * phase)).astype(np.complex64)
    return burst


def _frame_bits(payload: bytes, training: int = 128) -> str:
    packet = hf.build_packet(payload)
    return "10" * (training // 2) + hf.SYNC_BITS + "".join(f"{b:08b}" for b in packet)


def _stream(
    payloads: list[bytes],
    carriers: list[float],
    rate: float = 250_000.0,
    amplitude: float = 0.5,
    seed: int = 1,
    gap_s: float = 30.0,
) -> np.ndarray:
    """Noise with one burst per payload, ``gap_s`` apart, the first at 2 s."""
    rng = np.random.default_rng(seed)
    total = int(rate * (2 + gap_s * len(payloads) + 8))
    iq = (rng.standard_normal(total) + 1j * rng.standard_normal(total)).astype(np.complex64)
    iq *= np.float32(1 / np.sqrt(2))
    for i, (payload, carrier) in enumerate(zip(payloads, carriers, strict=True)):
        burst = _fsk_burst(_frame_bits(payload), rate, carrier, amplitude)
        start = int((2 + gap_s * i) * rate)
        iq[start : start + len(burst)] += burst
    return iq


def test_crc_known_value() -> None:
    """CRC-CCITT-FALSE check value from the standard test string."""
    assert hf.crc16_ccitt_false(b"123456789") == 0x29B1


def test_real_packet_is_verified_and_descrambled() -> None:
    assert hf.crc16_ccitt_false(REAL_RAW) == 0
    result = hf.parse_packet(REAL_RAW)
    assert result == (REAL_PAYLOAD, False)


def test_scramble_roundtrip() -> None:
    for payload in REAL_PAYLOADS:
        assert hf.descramble(hf.scramble(payload)) == payload


def test_build_packet_matches_real_bytes() -> None:
    assert hf.build_packet(REAL_PAYLOAD) == REAL_RAW


def test_wrong_address_is_refused() -> None:
    assert hf.parse_packet(REAL_RAW, address=3) is None
    assert hf.parse_packet(REAL_RAW, address=None) is not None


def test_single_bit_error_is_repaired_and_flagged() -> None:
    damaged = bytearray(REAL_RAW)
    damaged[12] ^= 0x10
    assert hf.parse_packet(bytes(damaged), allow_repair=False) is None
    assert hf.parse_packet(bytes(damaged)) == (REAL_PAYLOAD, True)


def test_two_bit_errors_are_not_accepted() -> None:
    damaged = bytearray(REAL_RAW)
    damaged[12] ^= 0x10
    damaged[20] ^= 0x01
    assert hf.parse_packet(bytes(damaged)) is None


def test_uptime_counter_property() -> None:
    frame = hf.HadesFrame(0, 0.0, 0.0, 0.0, 1, 12, REAL_PAYLOAD, False)
    assert frame.uptime_s == 0x0013FD7A
    other = hf.HadesFrame(0, 0.0, 0.0, 0.0, 15, 12, bytes([0xFC] + [0] * 38), False)
    assert other.uptime_s is None
    assert other.type_name == "Voice"  # UNNE-1 sends voice as type 15
    assert hf.HadesFrame(0, 0.0, 0.0, 0.0, 15, 13, b"", False).type_name == "BBS data"


def test_frames_from_bits_finds_packet_with_a_sync_bit_error() -> None:
    bits = _frame_bits(REAL_PAYLOAD)
    flipped = bits[:130] + ("0" if bits[130] == "1" else "1") + bits[131:]
    assert hf.frames_from_bits(flipped) == [(REAL_PAYLOAD, False)]


def test_frames_from_bits_inverted_polarity() -> None:
    inverted = hf._invert(_frame_bits(REAL_PAYLOAD))
    assert hf.frames_from_bits(inverted) == [(REAL_PAYLOAD, False)]


def test_noise_bits_give_no_frame() -> None:
    rng = np.random.default_rng(7)
    for _ in range(50):
        bits = "".join("01"[b] for b in rng.integers(0, 2, 400))
        assert hf.frames_from_bits(bits, allow_repair=False) == []


def test_stream_decodes_bursts_with_a_large_carrier_offset() -> None:
    """The TLE-error case: bursts 3-12 kHz away from the Doppler-corrected centre."""
    carriers = [3_100.0, 8_900.0, -11_500.0]
    iq = _stream(REAL_PAYLOADS, carriers)
    frames = hf.decode_iq(iq, 250_000.0)
    assert [f.payload for f in frames] == REAL_PAYLOADS
    assert not any(f.repaired for f in frames)
    for frame, carrier in zip(frames, carriers, strict=True):
        assert abs(frame.carrier_hz - carrier) < 1_500
    # The first burst starts at 2 s, the others 30 s apart.
    assert [round(f.time_s) for f in frames] == [2, 32, 62]


def test_stream_decodes_a_burst_too_weak_for_a_frequency_discriminator() -> None:
    """Tone-energy detection: a burst ~10 dB weaker than the other tests' (2026-10-28 UNNE-1B)."""
    for seed in (1, 2, 3):
        iq = _stream([REAL_PAYLOAD], [-1_200.0], amplitude=0.15, seed=seed)
        frames = hf.decode_iq(iq, 250_000.0)
        assert [f.payload for f in frames] == [REAL_PAYLOAD]
        assert not frames[0].repaired


def test_stream_reports_every_burst_once_for_any_chunk_size() -> None:
    iq = _stream(REAL_PAYLOADS[:2], [5_000.0, -4_000.0])
    reference = [f.payload for f in hf.decode_iq(iq, 250_000.0)]
    assert reference == REAL_PAYLOADS[:2]
    for chunk in (1000, 4097, 50_000):
        assert [f.payload for f in hf.decode_iq(iq, 250_000.0, chunk=chunk)] == reference


def test_stream_other_sample_rate() -> None:
    iq = _stream(REAL_PAYLOADS[:1], [6_000.0], rate=192_000.0)
    frames = hf.decode_iq(iq, 192_000.0)
    assert [f.payload for f in frames] == REAL_PAYLOADS[:1]


def test_stream_ignores_noise() -> None:
    rng = np.random.default_rng(3)
    n = 250_000 * 40
    iq = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
    assert hf.decode_iq(iq, 250_000.0) == []


def test_stream_rejects_too_low_a_sample_rate() -> None:
    try:
        hf.HadesFskStream(24_000.0)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_decimate_stage_matches_convolution() -> None:
    rng = np.random.default_rng(5)
    x = (rng.standard_normal(1000) + 1j * rng.standard_normal(1000)).astype(np.complex64)
    taps = hf._design_lowpass(250_000.0, 22_500.0, 81)
    out, rest = hf._decimate_stage(x, taps, 5)
    reference = np.convolve(x, taps, mode="valid")[::5]
    assert np.allclose(out, reference[: len(out)], atol=1e-5)
    assert len(out) == len(reference)
    # Feeding the stream in two pieces gives the same samples as feeding it whole.
    first, rest1 = hf._decimate_stage(x[:437], taps, 5)
    second, _ = hf._decimate_stage(np.concatenate([rest1, x[437:]]), taps, 5)
    assert np.allclose(np.concatenate([first, second]), out, atol=1e-5)
    assert len(rest) < len(taps) + 5


def test_split_factor() -> None:
    assert hf._split_factor(1) == []
    assert hf._split_factor(5) == [5]
    assert hf._split_factor(48) == [8, 6]
    assert np.prod(hf._split_factor(12)) == 12
    assert hf._split_factor(11) == [11]


def test_stream_high_sample_rate_uses_cascade() -> None:
    """A 600 kHz stream (decimation 12 in two stages) decodes like a 250 kHz one."""
    iq = _stream(REAL_PAYLOADS[:1], [-7_500.0], rate=600_000.0, gap_s=0.0)
    frames = hf.decode_iq(iq, 600_000.0)
    assert [f.payload for f in frames] == REAL_PAYLOADS[:1]
    assert abs(frames[0].carrier_hz + 7_500.0) < 1_500


def test_unne1_voice_packet_has_39_bytes_hades_r_41() -> None:
    voice = bytes([0xFC, 7]) + bytes(range(35))
    packet = hf.build_packet(voice)
    assert len(packet) == 39
    assert hf.parse_packet(packet) == (voice, False)  # address 12 (UNNE-1)
    assert hf.parse_packet(packet, address=None) == (voice, False)
    assert hf.packet_lengths(12)[15] == 39 and hf.packet_lengths(13)[15] == 41
    assert hf.packet_lengths(None)[15] == 41
    other = bytes([0xFD, 7]) + bytes(range(37))  # HADES-R (address 13): 41 bytes
    assert hf.parse_packet(hf.build_packet(other), address=13) == (other, False)
    assert hf.parse_packet(hf.build_packet(other), address=None) == (other, False)


def test_stream_decodes_a_voice_packet() -> None:
    voice = bytes([0xFC, 3]) + bytes(range(100, 135))
    iq = _stream([voice], [-6_000.0])
    assert [f.payload for f in hf.decode_iq(iq, 250_000.0)] == [voice]
