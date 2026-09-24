"""Tests for the coherent MSK/GMSK 4800/9600 baud G3RUH decoder (comms.aprs.coherent_msk)."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("scipy")

from scipy import signal as sp_signal  # noqa: E402

from comms.aprs.coherent_msk import (  # noqa: E402
    _CHUNK_S,
    CoherentMskDecoder,
    CoherentMskStream,
    crc16_x25,
    descramble_nrzi,
    hdlc_frames,
)

FS = 250_000


def _bits_lsb_first(data: bytes) -> list[int]:
    return [(b >> i) & 1 for b in data for i in range(8)]


def _hdlc_bits(frame: bytes, preamble_flags: int = 20) -> list[int]:
    flag = _bits_lsb_first(b"\x7e")
    out = flag * preamble_flags
    ones = 0
    for bit in _bits_lsb_first(frame):
        out.append(bit)
        if bit:
            ones += 1
            if ones == 5:
                out.append(0)
                ones = 0
        else:
            ones = 0
    return out + flag * 4


def _nrzi(bits: list[int]) -> list[int]:
    out, level = [], 0
    for b in bits:
        if b == 0:
            level ^= 1
        out.append(level)
    return out


def _scramble(bits: list[int]) -> list[int]:
    lfsr, out = 0, []
    for b in bits:
        x = (b ^ (lfsr >> 16) ^ (lfsr >> 11)) & 1
        lfsr = ((lfsr << 1) | x) & 0x1FFFF
        out.append(x)
    return out


def _frame_with_crc(payload: bytes) -> bytes:
    return payload + crc16_x25(payload).to_bytes(2, "little")


def _burst(payload: bytes, baud: int, bt: float = 0.5) -> np.ndarray:
    """Gaussian-filtered MSK (h=0.5) burst of one G3RUH-scrambled HDLC frame at FS."""
    bits = _scramble(_nrzi(_hdlc_bits(_frame_with_crc(payload))))
    sps = 25
    rate = baud * sps
    nrz = np.repeat(np.array(bits, dtype=float) * 2 - 1, sps)
    padded = np.concatenate([np.full(sps * 4, nrz[0]), nrz, np.full(sps * 4, nrz[-1])])
    sigma = np.sqrt(np.log(2)) / (2 * np.pi * bt) * sps
    n = int(sigma * 8) | 1
    t = np.arange(n) - n // 2
    g = np.exp(-(t**2) / (2 * sigma**2))
    g /= g.sum()
    phase = 2 * np.pi * np.cumsum(np.convolve(padded, g, "same")) * (baud / 4) / rate
    iq = np.exp(1j * phase).astype(np.complex64)
    return np.asarray(sp_signal.resample_poly(iq, FS // 1000, rate // 1000), dtype=np.complex64)


def _stream(
    payloads: list[bytes], baud: int, ebn0_db: float, gap_s: float = 0.6, seed: int = 1
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    parts: list[np.ndarray] = []
    for p in payloads:
        parts += [np.zeros(int(gap_s * FS), np.complex64), _burst(p, baud)]
    x = np.concatenate([*parts, np.zeros(int(gap_s * FS), np.complex64)])
    sigma2 = FS / (baud * 10 ** (ebn0_db / 10))
    noise = (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x))) * np.sqrt(sigma2 / 2)
    return (x + noise).astype(np.complex64)


def _payloads(n: int, length: int = 80, seed: int = 5) -> list[bytes]:
    rng = np.random.default_rng(seed)
    return [
        bytes([0x76, k]) + bytes(rng.integers(0, 256, length, dtype=np.uint8)) for k in range(n)
    ]


def _run(iq: np.ndarray, baud: int, block: int = 16384) -> list[bytes]:
    stream = CoherentMskStream(FS, baud)
    out: list[bytes] = []
    for i in range(0, len(iq), block):
        out += stream.feed(iq[i : i + block])
    out += stream.flush()
    return out


def test_crc16_x25_known_value() -> None:
    # CRC-16/X.25 check value from the CRC catalogue
    assert crc16_x25(b"123456789") == 0x906E


def test_hdlc_roundtrip_through_descramble_and_nrzi() -> None:
    payload = bytes([0x76, 1]) + bytes(range(90))
    line = np.array(_scramble(_nrzi(_hdlc_bits(_frame_with_crc(payload)))), dtype=np.uint8)
    frames = [f for f, _ in hdlc_frames(descramble_nrzi(line))]
    assert frames == [payload]


def test_hdlc_rejects_corrupted_frame() -> None:
    payload = bytes(range(60))
    bits = _hdlc_bits(_frame_with_crc(payload))
    bits[8 * 20 + 40] ^= 1  # flip one bit inside the body
    line = np.array(_scramble(_nrzi(bits)), dtype=np.uint8)
    assert hdlc_frames(descramble_nrzi(line)) == []


@pytest.mark.parametrize("baud", [4800, 9600])
def test_decodes_clean_frames(baud: int) -> None:
    payloads = _payloads(6)
    got = _run(_stream(payloads, baud, ebn0_db=25.0), baud)
    assert sorted(got) == sorted(payloads)


def test_decodes_at_low_snr_4800() -> None:
    # 50 % point of the coherent detector is ~8 dB Eb/N0; 11 dB must be nearly perfect.
    payloads = _payloads(10)
    got = set(_run(_stream(payloads, 4800, ebn0_db=11.0), 4800))
    assert len(got & set(payloads)) >= 8


def test_decodes_at_low_snr_9600() -> None:
    payloads = _payloads(10, length=120)
    got = set(_run(_stream(payloads, 9600, ebn0_db=12.0), 9600))
    assert len(got & set(payloads)) >= 8


def test_tolerates_carrier_offset() -> None:
    payloads = _payloads(6)
    iq = _stream(payloads, 4800, ebn0_db=20.0)
    iq = iq * np.exp(2j * np.pi * 300.0 * np.arange(len(iq)) / FS).astype(np.complex64)
    got = _run(iq, 4800)
    assert len(set(got) & set(payloads)) >= 5


def test_no_frames_from_noise() -> None:
    rng = np.random.default_rng(9)
    noise = (rng.standard_normal(6 * FS) + 1j * rng.standard_normal(6 * FS)).astype(np.complex64)
    assert _run(noise, 4800) == []
    assert _run(noise, 9600) == []


@pytest.mark.parametrize("block", [4096, 12345, 100_003])
def test_each_frame_emitted_once_regardless_of_block_size(block: int) -> None:
    payloads = _payloads(6)
    got = _run(_stream(payloads, 4800, ebn0_db=25.0), 4800, block=block)
    assert sorted(got) == sorted(payloads)


def test_frame_spanning_a_chunk_boundary_is_decoded_once() -> None:
    payloads = _payloads(1)
    # place the burst so it straddles the first chunk boundary
    lead = np.zeros(int((_CHUNK_S - 0.05) * FS), np.complex64)
    iq = np.concatenate([lead, _burst(payloads[0], 4800), np.zeros(4 * FS, np.complex64)])
    rng = np.random.default_rng(3)
    iq = iq + (rng.standard_normal(len(iq)) + 1j * rng.standard_normal(len(iq))).astype(
        np.complex64
    ) * np.float32(0.02)
    assert _run(iq, 4800) == payloads


def test_identical_frames_sent_twice_are_both_reported() -> None:
    payload = _payloads(1)[0]
    got = _run(_stream([payload, payload], 4800, ebn0_db=25.0, gap_s=1.0), 4800)
    assert got == [payload, payload]


def test_signal_gate_ignores_pure_noise() -> None:
    rng = np.random.default_rng(2)
    noise = (rng.standard_normal(FS) + 1j * rng.standard_normal(FS)).astype(np.complex64)
    assert not CoherentMskDecoder(FS, 4800).signal_present(noise)


def test_works_at_other_sdr_sample_rates() -> None:
    payloads = _payloads(4)
    iq = _stream(payloads, 4800, ebn0_db=25.0)
    iq_2m4 = np.asarray(sp_signal.resample_poly(iq, 48, 5), dtype=np.complex64)  # 2.4 Msps
    stream = CoherentMskStream(2_400_000, 4800)
    got: list[bytes] = []
    for i in range(0, len(iq_2m4), 65536):
        got += stream.feed(iq_2m4[i : i + 65536])
    got += stream.flush()
    assert sorted(got) == sorted(payloads)
