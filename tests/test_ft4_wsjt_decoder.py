"""
Unit tests for comms/ft4/wsjt_decoder.py — the WSJT-X-derived FT4 decode
engine (libft4wsjt: 3-pass signal subtraction + BP/OSD hybrid decode).

Skipped entirely when libft4wsjt is not installed (CI without the bundled
shared library, or a dev machine that hasn't run scripts/build_ft4wsjt.sh
yet), matching how the app itself degrades to the ft8_lib single-pass
fallback (see comms/ft4/codec.py).
"""

from __future__ import annotations

import numpy as np
import pytest

from comms.ft4.codec import FT4_PERIOD, SAMPLE_RATE, Ft4Codec
from comms.ft4.wsjt_decoder import Ft4WsjtDecoder, is_available

pytestmark = pytest.mark.skipif(
    not is_available(),
    reason="libft4wsjt not installed",
)

_tx_codec = Ft4Codec()
_decoder = Ft4WsjtDecoder(my_call="JF9SOM", ndepth=3)


def _make_test_buffer(message: str, offset_s: float, seed: int = 42) -> np.ndarray:
    """Encode `message` and place it inside a noisy 6-second FT4 period buffer.

    `offset_s` need not align to a 576-sample (48 ms) symbol boundary — a
    real received signal never does.
    """
    audio = _tx_codec.encode_audio(message, base_freq=800.0)
    assert audio is not None
    rng = np.random.default_rng(seed)
    period_samples = int(FT4_PERIOD * SAMPLE_RATE)
    buf = rng.normal(0, 0.02, period_samples).astype(np.float32)
    offset = int(offset_s * SAMPLE_RATE)
    buf[offset : offset + len(audio)] += audio * 0.3
    return buf


def _make_crowded_buffer(
    messages_freqs_amps: list[tuple[str, float, float]], seed: int
) -> np.ndarray:
    """Overlay several FT4 signals at different audio frequencies/amplitudes
    into a single noisy 6-second period buffer, simulating a busy satellite
    pass with many stations calling CQ simultaneously."""
    rng = np.random.default_rng(seed)
    period_samples = int(FT4_PERIOD * SAMPLE_RATE)
    buf = rng.normal(0, 0.08, period_samples).astype(np.float32)
    offset = int(0.5 * SAMPLE_RATE)  # FT4_TX_OFFSET
    for msg, freq, amp in messages_freqs_amps:
        audio = _tx_codec.encode_audio(msg, base_freq=freq)
        assert audio is not None
        end = offset + len(audio)
        buf[offset:end] += amp * audio
    peak = float(np.max(np.abs(buf)))
    if peak > 1.0:
        buf = buf / peak * 0.95
    return buf


@pytest.mark.parametrize("offset_s", [0.0, 0.123, 0.4567, 0.9])
def test_decode_at_non_symbol_aligned_offset(offset_s: float) -> None:
    """Decode must succeed regardless of where the signal starts."""
    buf = _make_test_buffer("CQ JF9SOM PM86", offset_s)
    messages = _decoder.decode_audio(buf)
    assert any(m.text == "CQ JF9SOM PM86" for m in messages)


def test_crowded_band_outperforms_or_matches_single_pass_fallback() -> None:
    """Regression test for the original bug report: on a real RS-44 pass,
    the ft8_lib single-pass fallback decoded only 3 stations while WSJT-X
    decoded many more. This reproduces a crowded, closely-spaced, weak-signal
    scenario synthetically and asserts the WSJT-X-derived engine (3-pass
    subtract + BP/OSD) recovers at least as many unique stations as the
    lightweight ft8_lib fallback, and in particular does not miss the
    weakest (lowest-amplitude) signals in the set.
    """
    messages = [
        ("CQ JF9SOM PM86", 800.0, 0.9),
        ("CQ JA1XYZ QM05", 860.0, 0.35),
        ("CQ W1ABC FN42", 920.0, 0.22),
        ("CQ VK2DEF QF56", 980.0, 0.16),
        ("CQ DL3GHI JO50", 1040.0, 0.12),
        ("CQ G0ABC IO91", 1100.0, 0.09),
        ("CQ VE3XYZ FN25", 1160.0, 0.07),
        ("CQ ZL1ABC RE66", 1220.0, 0.06),
        ("CQ PY2XYZ GG66", 1280.0, 0.05),
        ("CQ EA1ABC IN80", 1340.0, 0.045),
    ]
    buf = _make_crowded_buffer(messages, seed=7)

    wsjt_found = {m.text for m in _decoder.decode_audio(buf.copy())}
    ft8lib_found = {m.text for m in _tx_codec.decode_audio(buf.copy())}

    assert len(wsjt_found) >= len(ft8lib_found)
    # The weakest three signals are the ones most likely to expose the
    # single-pass decoder's lack of interference cancellation / OSD.
    for msg, _freq, _amp in messages[-3:]:
        assert msg in wsjt_found, f"WSJT-X engine missed weak signal: {msg}"
