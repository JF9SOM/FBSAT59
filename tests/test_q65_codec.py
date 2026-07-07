"""
Unit tests for comms/q65/codec.py's Q65Codec — backed by libq65, WSJT-X's
own Q65 decode engine (lib/q65_decode.f90), wrapped via
scripts/wsjtx_bridge/q65wsjt_bridge.f90.

Skipped entirely when libq65 is not installed (CI without the bundled
shared library, or a dev machine that hasn't run scripts/build_q65lib.sh
yet).
"""

from __future__ import annotations

import numpy as np
import pytest

from comms.q65 import encoder
from comms.q65.codec import Q65Codec, is_available

pytestmark = pytest.mark.skipif(
    not is_available(),
    reason="libq65 not installed",
)


def _make_test_buffer(
    message: str, period_seconds: int, offset_s: float, seed: int = 42
) -> np.ndarray:
    """Encode `message` and place it inside a noisy Q65 period buffer."""
    tones = encoder.get_q65_tones(message)
    audio = encoder.synthesize_audio(tones, submode="A", period_seconds=period_seconds, f0=1000.0)
    rng = np.random.default_rng(seed)
    period_samples = period_seconds * 12_000
    buf = rng.normal(0, 0.01, period_samples).astype(np.float32)
    offset = int(offset_s * 12_000)
    end = min(offset + len(audio), period_samples)
    buf[offset:end] += audio[: end - offset] * 0.3
    return buf


def test_decode_cq_message() -> None:
    """Round-trip: encode with our own Python TX encoder, decode via libq65."""
    buf = _make_test_buffer("CQ JF9SOM PM86", period_seconds=15, offset_s=1.0)
    codec = Q65Codec(submode="A", nfa=200, nfb=3000, nfqso=1000)
    messages = codec.decode(buf, period_seconds=15)
    assert any(m.text == "CQ JF9SOM PM86" for m in messages)


def test_decode_silence_returns_empty() -> None:
    """Silence should not produce spurious decodes."""
    buf = np.zeros(15 * 12_000, dtype=np.float32)
    codec = Q65Codec(submode="A", nfa=200, nfb=3000, nfqso=1000)
    messages = codec.decode(buf, period_seconds=15)
    assert messages == []
