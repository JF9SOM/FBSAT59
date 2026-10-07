"""G3RUH (scrambled NRZI baseband) AX.25 transmit waveform generator.

Direwolf cannot be used to transmit through this app: its ``ADEVICE stdin
stdout`` mode only takes receive audio from stdin; Direwolf never writes
transmit audio to stdout (its audio output always goes to a real sound device,
and none is opened in stdin mode), so a frame handed to it over KISS was
silently dropped while the app keyed the PTT -- an unmodulated carrier.

This module builds the transmit audio itself. It is a port of the Direwolf
1.8.1 chain used for ``MODEM 4800 G3RUH`` / ``MODEM 9600``:

  hdlc_send.c   flag preamble, bit-stuffed frame + CRC-16/X.25 FCS, flag tail,
                NRZI (0 = toggle, 1 = hold; flags are not stuffed)
  gen_tone.c    G3RUH scrambler x^17 + x^12 + 1, then the baseband waveform:
                the level (+1 for a 0 line bit, -1 for a 1) only changes when
                the scrambled bit differs from the previous one, and then as a
                half cosine spread over the whole bit; otherwise it is flat.

The result is a float array in -1..1 at *sample_rate*; the caller applies its
own TX level and plays it. A pure-numpy round trip (see tests) and a decode of
the same audio by Direwolf itself check the bit chain.
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

# Direwolf defaults: TXDELAY 30 (x10 ms) and TXTAIL 10 (x10 ms).
DEFAULT_TXDELAY_MS = 300.0
DEFAULT_TXTAIL_MS = 100.0


def fcs16(data: bytes) -> int:
    """CRC-16/X.25 (the AX.25 FCS) of *data*, to be sent low byte first."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def hdlc_nrzi_bits(frame: bytes, preamble_flags: int, tail_flags: int) -> list[int]:
    """Line bits (NRZI done) for *frame* between flag preamble and tail.

    The frame gets its FCS appended and is bit stuffed (a 0 after five 1s);
    flags are sent unstuffed. Every byte goes out least significant bit first.
    NRZI: a 0 data bit toggles the line, a 1 holds it; the line starts at 0.
    """
    line = 0
    out: list[int] = []

    def put(bit: int) -> None:
        nonlocal line
        if bit == 0:
            line ^= 1
        out.append(line)

    def put_flag() -> None:
        for i in range(8):
            put((0x7E >> i) & 1)

    for _ in range(preamble_flags):
        put_flag()
    put_flag()  # start flag
    ones = 0
    body = frame + fcs16(frame).to_bytes(2, "little")
    for byte in body:
        for i in range(8):
            bit = (byte >> i) & 1
            put(bit)
            if bit:
                ones += 1
                if ones == 5:
                    put(0)
                    ones = 0
            else:
                ones = 0
    put_flag()  # end flag
    for _ in range(tail_flags):
        put_flag()
    return out


def scramble(bits: list[int]) -> list[int]:
    """G3RUH scrambler (x^17 + x^12 + 1), shift register starting at zero."""
    lfsr = 0
    out: list[int] = []
    for bit in bits:
        x = (bit ^ (lfsr >> 16) ^ (lfsr >> 11)) & 1
        lfsr = ((lfsr << 1) | x) & 0x1FFFF
        out.append(x)
    return out


def _level(bit: int) -> float:
    """Baseband level a scrambled bit settles at (Direwolf: 0 -> +1, 1 -> -1)."""
    return -1.0 if bit else 1.0


def baseband_samples(bits: list[int], baud: int, sample_rate: int) -> NDArray[np.float32]:
    """Direwolf's G3RUH waveform for the scrambled *bits*.

    A bit equal to its predecessor is a flat level; a bit that differs moves the
    level to the new one along a half cosine over exactly that bit's samples
    (the phase advances baud/2 cycles per second, i.e. 180 degrees per bit).
    The sample count per bit follows the same accumulator as Direwolf, so the
    non-integer ratios stay in step.
    """
    step = math.pi * baud / sample_rate  # radians per sample: 180 deg per bit
    samples: list[float] = []
    prev = 0
    level = 1.0  # line state before the first bit (bit 0 -> +1)
    acc = 0.0
    for bit in bits:
        new_level = _level(bit)
        count = 0
        # Samples are produced until one bit time has been used up.
        while True:
            count += 1
            acc += 1.0 / sample_rate
            if acc >= 1.0 / baud - 1e-12:
                acc -= 1.0 / baud
                break
        if bit != prev:
            for k in range(1, count + 1):
                samples.append(level * math.cos(step * k))
        else:
            samples.extend([new_level] * count)
        level = new_level
        prev = bit
    return np.asarray(samples, dtype=np.float32)


def build_g3ruh_audio(
    frame: bytes,
    baud: int = 4800,
    sample_rate: int = 48_000,
    txdelay_ms: float = DEFAULT_TXDELAY_MS,
    txtail_ms: float = DEFAULT_TXTAIL_MS,
) -> NDArray[np.float32]:
    """Audio (-1..1) of one AX.25 *frame* (without FCS) for a G3RUH modem.

    *txdelay_ms* of flags before the frame let the radio and the satellite's
    demodulator settle; *txtail_ms* of flags follow it.
    """
    if baud <= 0 or sample_rate <= 0:
        raise ValueError("baud and sample_rate must be positive")
    bits_per_flag = 8
    pre = max(1, round(txdelay_ms * baud / (bits_per_flag * 1000.0)))
    tail = max(0, round(txtail_ms * baud / (bits_per_flag * 1000.0)))
    line = hdlc_nrzi_bits(frame, pre, tail)
    return baseband_samples(scramble(line), baud, sample_rate)
