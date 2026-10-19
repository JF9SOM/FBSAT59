"""Tests for the 2FSK 800 baud SSDV demodulator (comms.sstv.fsk_ssdv)."""

from __future__ import annotations

import zlib

import numpy as np
import pytest

pytest.importorskip("scipy")

from comms.sstv import rs_ssdv  # noqa: E402
from comms.sstv.fsk_ssdv import (  # noqa: E402
    PACKET_PREFIX,
    FskSsdvStream,
    crc_ok,
    find_tones,
    header_plausible,
)
from tests.test_ssdv import make_packet  # noqa: E402

FS = 250_000
BAUD = 800
LOW_HZ = -650.0  # bit 1 = the lower tone
HIGH_HZ = 950.0


def hades_packet(pid: int, image: int = 32) -> bytes:
    """A 256-byte SSDV packet with the HADES-SA callsign bytes (BF 35 FB A3) and a valid CRC."""
    pkt = bytearray(make_packet(image=image, pid=pid))
    pkt[2:6] = b"\xbf\x35\xfb\xa3"
    pkt[11] = 0x18  # quality 7, not the last packet
    pkt[220:224] = zlib.crc32(bytes(pkt[1:220])).to_bytes(4, "big")
    pkt[224:256] = rs_ssdv.encode(bytes(pkt[1:224]))  # real Reed-Solomon parity
    return bytes(pkt)


def _bits(data: bytes) -> list[int]:
    return [(b >> (7 - i)) & 1 for b in data for i in range(8)]


def fsk_iq(
    packets: list[bytes],
    *,
    lead_s: float = 1.0,
    gap_s: float = 0.4,
    snr_db: float = 20.0,
    offset_hz: float = 150.0,
    seed: int = 1,
) -> np.ndarray:
    """Continuous-phase 2FSK I/Q carrying *packets* (each sent as filler + sync + 252 bytes)."""
    rng = np.random.default_rng(seed)
    bits: list[int] = [1, 0] * int(lead_s * BAUD / 2)
    for pkt in packets:
        bits += _bits(b"\x55" * 8 + pkt[2:4] + pkt[4:])
        bits += _bits(b"\x55" * 4)
        bits += [1, 0] * int(gap_s * BAUD / 2)
    sym = np.array(bits)
    n = int(len(sym) * FS / BAUD)
    idx = (np.arange(n) * BAUD // FS).clip(max=len(sym) - 1)
    freq = np.where(sym[idx] == 1, LOW_HZ, HIGH_HZ) + offset_hz
    phase = 2 * np.pi * np.cumsum(freq) / FS
    sig = np.exp(1j * phase)
    sigma = 10 ** (-snr_db / 20) / np.sqrt(2)
    noise = sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    return (sig + noise).astype(np.complex64)


def _run(iq: np.ndarray, block: int = FS // 4) -> list:
    stream = FskSsdvStream(FS)
    out = []
    for a in range(0, len(iq), block):
        out.extend(stream.feed(iq[a : a + block]))
    # push silence so the last window completes
    silence = np.zeros(8 * FS, dtype=np.complex64)
    for a in range(0, len(silence), block):
        out.extend(stream.feed(silence[a : a + block]))
    return out


def test_prefix_makes_a_standard_packet() -> None:
    pkt = hades_packet(3)
    assert pkt[:4] == PACKET_PREFIX
    assert crc_ok(pkt)
    assert header_plausible(pkt)
    bad = bytearray(pkt)
    bad[100] ^= 0x10
    assert not crc_ok(bytes(bad))


def test_find_tones_locates_the_pair() -> None:
    iq = fsk_iq([hades_packet(0)])
    from scipy import signal as sp_signal

    y = sp_signal.resample_poly(iq[: 7 * FS], 1, 20).astype(np.complex64)
    tones = find_tones(y[: 6 * 12500])
    assert tones is not None
    assert tones[0] == pytest.approx(LOW_HZ + 150.0, abs=40.0)
    assert tones[1] == pytest.approx(HIGH_HZ + 150.0, abs=40.0)


def test_decodes_packets_from_streamed_iq() -> None:
    sent = [hades_packet(pid) for pid in range(4)]
    got = _run(fsk_iq(sent))
    verified = [p.data for p in got if p.verified]
    assert verified == sent


def test_decodes_with_noise_and_a_tuning_offset() -> None:
    sent = [hades_packet(pid) for pid in range(3)]
    got = _run(fsk_iq(sent, snr_db=8.0, offset_hz=-900.0, seed=3))
    assert {p.data for p in got if p.verified} == set(sent)


def test_block_size_does_not_matter() -> None:
    sent = [hades_packet(pid) for pid in range(2)]
    iq = fsk_iq(sent)
    for block in (1000, 40_000, 333_333):
        got = _run(iq, block=block)
        assert [p.data for p in got if p.verified] == sent


def test_noise_only_gives_no_packets() -> None:
    rng = np.random.default_rng(5)
    iq = (rng.standard_normal(12 * FS) + 1j * rng.standard_normal(12 * FS)).astype(np.complex64)
    assert _run(iq) == []


def test_file_decoder_finds_the_packets_of_a_recording(qtbot, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from comms.sstv.fsk_ssdv import FskSsdvFileDecoder
    from tests.test_iq_wav_fast_decode import _write_wav

    packets = [hades_packet(i) for i in range(3)]
    path = tmp_path / "rec.iq.wav"
    _write_wav(path, fsk_iq(packets), rate=FS)
    job = FskSsdvFileDecoder(path)
    found: list[bytes] = []
    job.packet_found.connect(lambda data, _ok: found.append(data))
    with qtbot.waitSignal(job.decoded, timeout=60_000) as done:
        job.start()
    assert found == packets
    assert done.args[2] == 3
    job.wait(3000)


def test_file_decoder_reports_a_bad_file(qtbot, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from comms.sstv.fsk_ssdv import FskSsdvFileDecoder

    bad = tmp_path / "x.wav"
    bad.write_bytes(b"junk")
    job = FskSsdvFileDecoder(bad)
    with qtbot.waitSignal(job.failed, timeout=10_000):
        job.start()
    job.wait(3000)
