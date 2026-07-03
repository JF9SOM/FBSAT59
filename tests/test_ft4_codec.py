"""
Unit tests for comms/ft4/codec.py — waterfall computation and decode round-trip.

Skipped entirely when ft8_lib's decode API is not installed (CI without the
bundled shared library), matching how the app itself degrades.
"""

from __future__ import annotations

import numpy as np
import pytest

from comms.ft4.codec import FT4_PERIOD, SAMPLE_RATE, Ft4Codec, compute_waterfall

_codec = Ft4Codec()

pytestmark = pytest.mark.skipif(
    not _codec.decode_available,
    reason="ft8_lib decode API not installed",
)


def _make_test_buffer(message: str, offset_s: float, seed: int = 42) -> np.ndarray:
    """Encode `message` and place it inside a noisy 6-second FT4 period buffer.

    `offset_s` need not align to a 576-sample (48 ms) symbol boundary — a
    real received signal never does, since neither the far station's TX
    start time nor propagation delay is known to sub-symbol precision.
    """
    audio = _codec.encode_audio(message, base_freq=800.0)
    assert audio is not None
    rng = np.random.default_rng(seed)
    period_samples = int(FT4_PERIOD * SAMPLE_RATE)
    buf = rng.normal(0, 0.02, period_samples).astype(np.float32)
    offset = int(offset_s * SAMPLE_RATE)
    buf[offset : offset + len(audio)] += audio * 0.3
    return buf


@pytest.mark.parametrize("offset_s", [0.0, 0.123, 0.4567, 0.9])
def test_decode_at_non_symbol_aligned_offset(offset_s: float) -> None:
    """Decode must succeed regardless of where the signal starts.

    Regression test for the missing time/frequency oversampling in
    compute_waterfall(): with time_osr=freq_osr=1 (the pre-fix behavior),
    ftx_find_candidates() only synchronizes when the signal happens to start
    exactly on a 48 ms block boundary, which never happens with a real
    over-the-air signal.
    """
    buf = _make_test_buffer("CQ JF9SOM PM86", offset_s)
    messages = _codec.decode_audio(buf)
    assert any(m.text == "CQ JF9SOM PM86" for m in messages)


def test_compute_waterfall_shape() -> None:
    """mag array shape must match ftx_waterfall_t's block_stride layout."""
    buf = _make_test_buffer("CQ JF9SOM PM86", 0.2)
    mag, num_blocks, num_bins, bin_hz = compute_waterfall(buf)
    assert num_blocks == len(buf) // 576
    assert mag.shape == (num_blocks, 2 * 2 * num_bins)  # time_osr=2, freq_osr=2
    assert bin_hz > 0
