"""
End-to-end test for comms/ax100digi/gmsk_demod.py: synthesize a GMSK-like
signal for a known AX100 frame, run it through the discriminator + fixed-
phase symbol slicer, and confirm the original frame decodes back out.

This is a synthetic-signal test (Gaussian-shaped CPFSK, no channel noise,
no real transceiver/SDR chain) — it validates that the DSP pipeline's
math is wired together correctly, not that the exact deviation/bandwidth
constants match a real captured MARMOTSat/GreenCube signal (see
CLAUDE.md's Phase 0/1 plan for that).
"""

from __future__ import annotations

import numpy as np
import pytest

from comms.ax100digi import frame, gmsk_demod, rs_ccsds
from comms.ax100digi.csp import CspHeader, build_csp_packet

pytestmark = pytest.mark.skipif(
    not rs_ccsds.is_available(),
    reason="reed-solomon-ccsds not installed",
)

_SAMPLE_RATE = 48_000.0
_BAUD = 1200.0
_SPS_TX = int(_SAMPLE_RATE / _BAUD)  # 40, exact for this test's sample rate


def _gaussian_kernel(bt: float, sps: int, span_symbols: int = 4) -> np.ndarray:
    t = np.arange(-span_symbols * sps // 2, span_symbols * sps // 2 + 1) / sps
    alpha = np.sqrt(np.log(2)) / (2 * np.pi * bt)
    h = np.exp(-(t**2) / (2 * alpha**2))
    return h / np.sum(h)


def _synthesize_gmsk_iq(
    bits: np.ndarray, deviation_hz: float = gmsk_demod.DEFAULT_DEVIATION_HZ, bt: float = 0.3
) -> np.ndarray:
    nrz = 2.0 * bits.astype(np.float64) - 1.0
    upsampled = np.repeat(nrz, _SPS_TX)
    kernel = _gaussian_kernel(bt, _SPS_TX)
    shaped = np.convolve(upsampled, kernel, mode="same")
    freq = shaped * deviation_hz
    phase = 2 * np.pi * np.cumsum(freq) / _SAMPLE_RATE
    return np.exp(1j * phase).astype(np.complex64)


def _decode_via_gmsk_chain(iq: np.ndarray) -> list[frame.Ax100Frame]:
    demod = gmsk_demod.GmskDiscriminator(input_rate=_SAMPLE_RATE, baud=_BAUD)
    discrim = demod.process(iq)
    oversampled = gmsk_demod.resample_to_sps(discrim, _SAMPLE_RATE, _BAUD)
    candidates = gmsk_demod.slice_bits_all_phases(oversampled)

    found: list[frame.Ax100Frame] = []
    for candidate_bits in candidates:
        found.extend(frame.find_frames(candidate_bits))
    return found


def test_gmsk_chain_recovers_encoded_frame() -> None:
    header = CspHeader(priority=1, source=5, destination=10, dest_port=30, source_port=20)
    payload = build_csp_packet(header, b"MARMOTSat digipeater test message")

    bits = frame.encode_frame(payload, scrambler=True, rs=True)
    # A little run-in of alternating bits before the sync word lets the
    # discriminator's IF filter settle, mirroring a real receiver seeing a
    # carrier/preamble before the frame proper.
    preamble = np.tile(np.array([0, 1], dtype=np.uint8), 50)
    bits_with_preamble = np.concatenate([preamble, bits])

    iq = _synthesize_gmsk_iq(bits_with_preamble)
    decoded = _decode_via_gmsk_chain(iq)

    payloads = [f.payload for f in decoded]
    assert payload in payloads


def test_gmsk_chain_rejects_pure_noise() -> None:
    rng = np.random.default_rng(7)
    noise_bits = rng.integers(0, 2, size=4000, dtype=np.uint8)
    iq = _synthesize_gmsk_iq(noise_bits)
    decoded = _decode_via_gmsk_chain(iq)
    assert decoded == []
