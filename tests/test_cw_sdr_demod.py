"""Tests for comms/cw/sdr_demod.py -- the CW Decoder tab's private CW-mode
demodulator on the SDR pipeline's raw I/Q.

The tab used to consume SDRPipeline.audio_ready, which follows SDR Control's
demodulation mode (USB by default), so a CW carrier below the tuned frequency
never reached the decoder. These tests pin the properties the tab relies on:
the output is 48 kHz PCM, and a carrier on either side of 0 Hz comes out as
an audible tone.
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from PySide6.QtCore import Qt

pytest.importorskip("scipy")

from comms.cw.sdr_demod import SDR_AUDIO_RATE, CwSdrDemod  # noqa: E402 -- after importorskip

_RATE = 250_000
_BLOCK = 16_384  # SDRPipeline's block size


def _carrier(offset_hz: float, seconds: float) -> np.ndarray:
    n = int(_RATE * seconds)
    iq: np.ndarray = np.exp(2j * np.pi * offset_hz * np.arange(n) / _RATE).astype(np.complex64)
    return iq


def _run_demod(iq: np.ndarray) -> np.ndarray:
    """Push *iq* through a CwSdrDemod thread and return everything it emitted."""
    demod = CwSdrDemod(_RATE)
    chunks: list[np.ndarray] = []
    # DirectConnection: the emit happens on the worker thread and this test
    # has no event loop to deliver a queued call.
    demod.audio_ready.connect(
        lambda c: chunks.append(np.asarray(c, dtype=np.float32)),
        Qt.ConnectionType.DirectConnection,
    )
    demod.start()
    for i in range(0, len(iq), _BLOCK):
        demod.push_samples(iq[i : i + _BLOCK])
    deadline = time.monotonic() + 10.0
    while not demod._q.empty() and time.monotonic() < deadline:
        time.sleep(0.01)
    demod.stop()
    assert chunks, "demodulator emitted no audio"
    return np.concatenate(chunks)


def _tone_hz(audio: np.ndarray) -> float:
    x = audio[len(audio) // 4 :]
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x)), 1 << 18))
    return float(np.argmax(spectrum) * SDR_AUDIO_RATE / (1 << 18))


def test_output_is_exactly_48khz_pcm() -> None:
    audio = _run_demod(_carrier(-900.0, 4.0))
    assert SDR_AUDIO_RATE == 48_000
    # 4 s of I/Q -> ~4 s of 48 kHz audio (a partial trailing block is allowed).
    assert abs(len(audio) / SDR_AUDIO_RATE - 4.0) < 0.35


@pytest.mark.parametrize("offset_hz", [-927.0, 927.0])
def test_carrier_on_either_side_of_zero_becomes_a_tone(offset_hz: float) -> None:
    """CW mode uses the real part, so a carrier below the tuned frequency
    (ARICA-2 sits ~927 Hz low) is heard just like one above it -- unlike USB,
    which rejects it."""
    audio = _run_demod(_carrier(offset_hz, 4.0))
    assert abs(_tone_hz(audio) - 927.0) < 15.0
