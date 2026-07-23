"""
Tests for comms/ax100digi/audio_bridge.py — the real-audio <-> complex-
baseband bridge used for Rig + Sound Card (SSB) reception and transmission.
"""

from __future__ import annotations

import numpy as np
import pytest

from comms.ax100digi import frame, gmsk_demod, rs_ccsds
from comms.ax100digi.audio_bridge import (
    AnalyticSignalConverter,
    FrequencyShifter,
    RxAudioBridge,
    synthesize_gmsk_audio,
)
from comms.ax100digi.csp import CspHeader, build_csp_packet

pytestmark = pytest.mark.skipif(
    not rs_ccsds.is_available(),
    reason="reed-solomon-ccsds not installed",
)

_SAMPLE_RATE = 48_000.0
_BAUD = 1200.0
_SHIFT_HZ = 1600.0


def test_analytic_signal_isolates_positive_frequency() -> None:
    # A real cosine at f0 becomes (after the analytic-signal Hilbert bridge)
    # a complex exponential rotating at +f0 Hz — i.e. the negative-frequency
    # image is suppressed, which is the whole point of using an analytic
    # signal instead of the raw real audio.
    f0 = 1000.0
    n = np.arange(4000)
    audio = np.cos(2 * np.pi * f0 * n / _SAMPLE_RATE).astype(np.float32)

    converter = AnalyticSignalConverter()
    analytic = converter.process(audio)

    # Measure the instantaneous frequency from the analytic signal's phase
    # progression (skip the FIR filter's settling region at both ends).
    settle = 200
    phase = np.unwrap(np.angle(analytic[settle:-settle]))
    freq = np.diff(phase) * _SAMPLE_RATE / (2 * np.pi)
    assert np.mean(freq) == pytest.approx(f0, abs=5.0)


def test_frequency_shifter_is_phase_continuous_across_chunks() -> None:
    iq = np.ones(1000, dtype=np.complex64)

    # Feed in two chunks vs. one chunk of the same total length — the
    # output must be identical (continuous phase accumulator), otherwise
    # chunk boundaries would produce audible/decodable-breaking clicks.
    shifter_a = FrequencyShifter(shift_hz=100.0, sample_rate=_SAMPLE_RATE)
    out_a1 = shifter_a.process(iq[:400])
    out_a2 = shifter_a.process(iq[400:])
    out_a = np.concatenate([out_a1, out_a2])

    shifter_b = FrequencyShifter(shift_hz=100.0, sample_rate=_SAMPLE_RATE)
    out_b = shifter_b.process(iq)

    np.testing.assert_allclose(out_a, out_b, atol=1e-5)


def test_tx_audio_then_rx_bridge_recovers_payload() -> None:
    header = CspHeader(priority=1, source=1, destination=5, dest_port=10, source_port=20)
    payload = build_csp_packet(header, b"rig soundcard loopback test")
    bits = frame.encode_frame(payload, scrambler=True, rs=True)

    # A short tail after the frame is needed, not just a leading preamble:
    # resample_to_sps()'s polyphase resampler has a small transient at the
    # very end of the buffer, so a signal with *zero* margin after the last
    # bit can lose the last symbol or two. Real transmissions always have
    # this margin for free (GreenCube's config.ini KeyDownDelay keeps PTT
    # keyed for 500ms after the last audio sample).
    preamble = np.tile(np.array([0, 1], dtype=np.uint8), 50)
    tail = np.tile(np.array([0, 1], dtype=np.uint8), 20)
    full_bits = np.concatenate([preamble, bits, tail])

    audio = synthesize_gmsk_audio(
        full_bits,
        sample_rate=_SAMPLE_RATE,
        baud=_BAUD,
        deviation_hz=gmsk_demod.DEFAULT_DEVIATION_HZ,
        shift_hz=_SHIFT_HZ,
    )

    bridge = RxAudioBridge(sample_rate=_SAMPLE_RATE, shift_hz=_SHIFT_HZ)
    iq = bridge.process(audio)

    demod = gmsk_demod.GmskDiscriminator(input_rate=_SAMPLE_RATE, baud=_BAUD)
    discrim = demod.process(iq)
    oversampled = gmsk_demod.resample_to_sps(discrim, _SAMPLE_RATE, _BAUD)
    candidates = gmsk_demod.slice_bits_all_phases(oversampled)

    decoded_payloads = []
    for candidate_bits in candidates:
        decoded_payloads.extend(f.payload for f in frame.find_frames(candidate_bits))

    assert payload in decoded_payloads


def test_tx_audio_then_rx_bridge_recovers_payload_via_chunked_feed() -> None:
    """Same as above but fed through the RxAudioBridge in small chunks, the
    way a real sound-card callback would deliver it."""
    header = CspHeader(priority=0, source=2, destination=6, dest_port=11, source_port=21)
    payload = build_csp_packet(header, b"chunked feed test")
    bits = frame.encode_frame(payload)
    preamble = np.tile(np.array([0, 1], dtype=np.uint8), 50)
    tail = np.tile(np.array([0, 1], dtype=np.uint8), 20)
    audio = synthesize_gmsk_audio(
        np.concatenate([preamble, bits, tail]),
        sample_rate=_SAMPLE_RATE,
        baud=_BAUD,
        deviation_hz=gmsk_demod.DEFAULT_DEVIATION_HZ,
        shift_hz=_SHIFT_HZ,
    )

    bridge = RxAudioBridge(sample_rate=_SAMPLE_RATE, shift_hz=_SHIFT_HZ)
    demod = gmsk_demod.GmskDiscriminator(input_rate=_SAMPLE_RATE, baud=_BAUD)

    discrim_chunks = []
    chunk_size = 1024
    for start in range(0, len(audio), chunk_size):
        chunk = audio[start : start + chunk_size]
        iq_chunk = bridge.process(chunk)
        discrim_chunks.append(demod.process(iq_chunk))
    discrim = np.concatenate(discrim_chunks)

    oversampled = gmsk_demod.resample_to_sps(discrim, _SAMPLE_RATE, _BAUD)
    candidates = gmsk_demod.slice_bits_all_phases(oversampled)
    decoded_payloads = []
    for candidate_bits in candidates:
        decoded_payloads.extend(f.payload for f in frame.find_frames(candidate_bits))

    assert payload in decoded_payloads
