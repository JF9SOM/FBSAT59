"""Bell 202 AFSK (1200 baud) transmit waveform for AX.25 / APRS.

Direwolf cannot transmit through this app (see g3ruh_tx.py: it never writes
transmit audio to stdout), so the audio is built here, following Direwolf's
``hdlc_send.c`` / ``gen_tone.c`` for ``MODEM 1200``:

  * flags, bit-stuffed frame + CRC-16/X.25 FCS, flags, with NRZI (shared with the
    G3RUH path: :func:`comms.aprs.g3ruh_tx.hdlc_nrzi_bits`)
  * the NRZI line bit selects the tone: 1 = mark (1200 Hz), 0 = space (2200 Hz),
    with a continuous phase across bit boundaries

The result is a float array in -1..1; the caller applies its own TX level.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from comms.aprs.g3ruh_tx import DEFAULT_TXDELAY_MS, DEFAULT_TXTAIL_MS, hdlc_nrzi_bits

MARK_HZ = 1200.0
SPACE_HZ = 2200.0


def build_afsk_audio(
    frame: bytes,
    baud: int = 1200,
    sample_rate: int = 48_000,
    txdelay_ms: float = DEFAULT_TXDELAY_MS,
    txtail_ms: float = DEFAULT_TXTAIL_MS,
) -> NDArray[np.float32]:
    """Audio (-1..1) of one AX.25 *frame* (without FCS) as Bell 202 AFSK."""
    if baud <= 0 or sample_rate <= 0:
        raise ValueError("baud and sample_rate must be positive")
    pre = max(1, round(txdelay_ms * baud / 8000.0))
    tail = max(0, round(txtail_ms * baud / 8000.0))
    line = hdlc_nrzi_bits(frame, pre, tail)

    # Samples per bit follow an accumulator so a non-integer ratio stays in step.
    counts = np.empty(len(line), dtype=np.int64)
    acc = 0.0
    for i in range(len(line)):
        n = 0
        while True:
            n += 1
            acc += 1.0 / sample_rate
            if acc >= 1.0 / baud - 1e-12:
                acc -= 1.0 / baud
                break
        counts[i] = n
    freq = np.repeat(np.where(np.asarray(line) == 1, MARK_HZ, SPACE_HZ), counts)
    phase = np.cumsum(2.0 * np.pi * freq / sample_rate)
    return np.sin(phase).astype(np.float32)
