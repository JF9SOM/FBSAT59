"""End-to-end test for comms/ax100digi/engine.py's Ax100DigiReceiver, using
the same synthetic GMSK signal approach as test_ax100digi_gmsk_demod.py."""

from __future__ import annotations

import numpy as np
import pytest

from comms.ax100digi import frame, rs_ccsds
from comms.ax100digi.csp import CspHeader, build_csp_packet
from comms.ax100digi.engine import Ax100DigiReceiver
from comms.ax100digi.message import build_message

pytestmark = pytest.mark.skipif(
    not rs_ccsds.is_available(),
    reason="reed-solomon-ccsds not installed",
)

_SAMPLE_RATE = 48_000.0
_BAUD = 1200.0
_SPS_TX = int(_SAMPLE_RATE / _BAUD)


def _gaussian_kernel(bt: float, sps: int, span_symbols: int = 4) -> np.ndarray:
    t = np.arange(-span_symbols * sps // 2, span_symbols * sps // 2 + 1) / sps
    alpha = np.sqrt(np.log(2)) / (2 * np.pi * bt)
    h = np.exp(-(t**2) / (2 * alpha**2))
    return h / np.sum(h)


def _synthesize_gmsk_iq(
    bits: np.ndarray, deviation_hz: float = 300.0, bt: float = 0.3
) -> np.ndarray:
    nrz = 2.0 * bits.astype(np.float64) - 1.0
    upsampled = np.repeat(nrz, _SPS_TX)
    kernel = _gaussian_kernel(bt, _SPS_TX)
    shaped = np.convolve(upsampled, kernel, mode="same")
    freq = shaped * deviation_hz
    phase = 2 * np.pi * np.cumsum(freq) / _SAMPLE_RATE
    return np.exp(1j * phase).astype(np.complex64)


def test_receiver_decodes_message_fed_in_chunks() -> None:
    text = build_message("IU0POY", "IU0BFO", "MARMOTSat", "engine test", store_seconds=0)
    header = CspHeader(priority=0, source=1, destination=2, dest_port=10, source_port=20)
    payload = build_csp_packet(header, text.encode("ascii"))

    bits = frame.encode_frame(payload)
    preamble = np.tile(np.array([0, 1], dtype=np.uint8), 50)
    full_bits = np.concatenate([preamble, bits])
    iq = _synthesize_gmsk_iq(full_bits)

    receiver = Ax100DigiReceiver(sample_rate=_SAMPLE_RATE, baud=_BAUD)

    # Feed in several small chunks, mimicking real SDR pipeline callbacks.
    chunk_size = 4096
    for start in range(0, len(iq), chunk_size):
        receiver.push_samples(iq[start : start + chunk_size])

    decoded = receiver.decode_pending()
    assert len(decoded) == 1
    result = decoded[0]
    assert result.payload == payload
    assert result.message is not None
    assert result.message.content == "engine test"
    assert result.message.sat_name == "MARMOTSat"


def test_receiver_does_not_redeliver_same_frame_twice() -> None:
    payload = build_csp_packet(
        CspHeader(priority=0, source=1, destination=2, dest_port=10, source_port=20),
        b"dedupe check",
    )
    bits = frame.encode_frame(payload)
    preamble = np.tile(np.array([0, 1], dtype=np.uint8), 50)
    iq = _synthesize_gmsk_iq(np.concatenate([preamble, bits]))

    receiver = Ax100DigiReceiver(sample_rate=_SAMPLE_RATE, baud=_BAUD)
    receiver.push_samples(iq)

    first = receiver.decode_pending()
    assert len(first) == 1

    # Same buffered data is still present (buffer isn't cleared between
    # calls); decode_pending() must not report it again.
    second = receiver.decode_pending()
    assert second == []
