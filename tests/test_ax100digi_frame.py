"""
Unit tests for comms/ax100digi/frame.py's AX100 "ASM+Golay" (u482c, mode='ASM')
frame codec — the framing GreenCube (IO-117) and MARMOTSat's VHF digipeater
both use.

These are self-encode/self-decode roundtrip tests (no real captured signal
available yet); they validate that the protocol layers (sync search, Golay
length field, CCSDS descrambler, RS(255,223)) are wired together correctly,
not that the exact on-air parameters match the real satellite bit-for-bit
(that requires a real IQ capture — see CLAUDE.md's Phase 0 plan).
"""

from __future__ import annotations

import numpy as np
import pytest

from comms.ax100digi import frame, rs_ccsds
from comms.ax100digi.csp import CspHeader, build_csp_packet, split_csp_packet
from comms.ax100digi.message import build_message, parse_message

pytestmark = pytest.mark.skipif(
    not rs_ccsds.is_available(),
    reason="reed-solomon-ccsds not installed",
)


def _make_csp_payload(text: str) -> bytes:
    header = CspHeader(priority=1, source=5, destination=10, dest_port=30, source_port=20)
    return build_csp_packet(header, text.encode("ascii"))


def test_encode_decode_roundtrip_clean_channel() -> None:
    text = build_message("IU0POY", "IU0BFO", "MARMOTSat", "hello from the ground", store_seconds=5)
    payload = _make_csp_payload(text)

    bits = frame.encode_frame(payload, scrambler=True, rs=True)
    frames = list(frame.find_frames(bits))

    assert len(frames) == 1
    decoded = frames[0]
    assert decoded.payload == payload
    assert decoded.scrambler_used is True
    assert decoded.rs_used is True
    assert decoded.golay_bit_errors == 0

    _header, decoded_body = split_csp_packet(decoded.payload)
    parsed = parse_message(decoded_body.decode("ascii"))
    assert parsed is not None
    assert parsed.content == "hello from the ground"
    assert parsed.sat_name == "MARMOTSat"


def test_encode_decode_roundtrip_without_scrambler_or_rs() -> None:
    payload = _make_csp_payload("raw beacon telemetry, no FEC")
    bits = frame.encode_frame(payload, scrambler=False, rs=False)
    frames = list(frame.find_frames(bits, require_rs=False))

    assert len(frames) == 1
    assert frames[0].payload == payload
    assert frames[0].scrambler_used is False
    assert frames[0].rs_used is False


def test_corrects_bit_errors_within_channel_via_rs_and_golay() -> None:
    payload = _make_csp_payload("robustness check against channel noise")
    bits = frame.encode_frame(payload, scrambler=True, rs=True)

    # Flip a handful of bits well inside the RS-protected payload region
    # (leave the sync word and Golay field untouched so this test isolates
    # RS correction behaviour).
    rng = np.random.default_rng(1234)
    payload_start = frame.SYNCWORD_LEN + 24
    flip_positions = rng.choice(np.arange(payload_start, len(bits)), size=8, replace=False)
    corrupted = bits.copy()
    corrupted[flip_positions] ^= 1

    frames = list(frame.find_frames(corrupted))
    assert len(frames) == 1
    assert frames[0].payload == payload
    assert frames[0].rs_bytes_corrected is not None
    assert frames[0].rs_bytes_corrected > 0


def test_finds_multiple_consecutive_frames() -> None:
    payload_a = _make_csp_payload("first frame")
    payload_b = _make_csp_payload("second frame")
    bits_a = frame.encode_frame(payload_a)
    bits_b = frame.encode_frame(payload_b)

    combined = np.concatenate([bits_a, bits_b])
    frames = list(frame.find_frames(combined))

    assert len(frames) == 2
    assert frames[0].payload == payload_a
    assert frames[1].payload == payload_b
    assert frames[1].bit_offset > frames[0].bit_offset


def test_ignores_noise_with_no_valid_syncword() -> None:
    rng = np.random.default_rng(99)
    noise = rng.integers(0, 2, size=2000, dtype=np.uint8)
    frames = list(frame.find_frames(noise))
    assert frames == []


def test_default_require_rs_rejects_rs_disabled_frame() -> None:
    """The new default (require_rs=True) rejects any frame whose Golay-
    decoded rs_flag is False — even a clean, error-free one — since
    GreenCube/MARMOTSat's real stack always uses RS(255,223); see
    find_frames()'s docstring for why an RS-disabled frame is far more
    likely to be a noise-driven false positive than a genuine one from
    this satellite family."""
    payload = _make_csp_payload("rs disabled frame")
    bits = frame.encode_frame(payload, scrambler=True, rs=False)

    assert list(frame.find_frames(bits)) == []  # default require_rs=True
    frames = list(frame.find_frames(bits, require_rs=False))
    assert len(frames) == 1
    assert frames[0].payload == payload


def test_require_rs_suppresses_false_positives_that_require_rs_false_lets_through() -> None:
    """Demonstrates the actual bug report's mechanism: a large noise buffer
    can produce spurious Golay "successes" whose rs_flag happens to be 0
    (accepting whatever random bytes follow with zero further validation)
    when require_rs=False, but require_rs=True (the default) rejects all
    of them since real Reed-Solomon protection essentially never passes
    on random data."""
    # Empirically confirmed (2026-07): with this seed/size, require_rs=False
    # finds 4 spurious frames (all rs_used=False); 19 of 20 seeds tried at
    # this size produced at least one. require_rs=True finds zero in every
    # one of those 20 seeds.
    rng = np.random.default_rng(0)
    noise = rng.integers(0, 2, size=2_000_000, dtype=np.uint8)

    assert list(frame.find_frames(noise)) == []  # default require_rs=True

    lenient_frames = list(frame.find_frames(noise, require_rs=False))
    assert any(not f.rs_used for f in lenient_frames)


def test_rejects_oversized_payload_for_rs() -> None:
    oversized = bytes(300)
    with pytest.raises(ValueError):
        frame.encode_frame(oversized, rs=True)
