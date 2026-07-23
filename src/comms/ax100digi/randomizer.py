"""CCSDS byte-synchronous pseudo-randomizer used by AX100's u482c "ASM" mode.

Bit-exact port of gr-satellites' lib/randomizer.c (Johan Christiansen /
Jeppe Ledet-Pedersen, MIT). Generator polynomial h(x) = x^8+x^7+x^5+x^3+1.
Applied as a plain byte-wise XOR against a fixed, precomputed sequence
(this is the "CCSDS randomization" the GreenCube manual refers to as
"Same as G3RUH 9k6 scrambler" — not to be confused with the bit-serial
LFSR descramblers elsewhere in this project).
"""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=4)
def ccsds_generate_sequence(length: int) -> bytes:
    """Generate `length` bytes of the CCSDS pseudo-random sequence."""
    x = [1, 1, 1, 1, 1, 1, 1, 1, 1]  # x[0..8], matches randomizer.c's char x[9]
    seq = bytearray(length)
    for i in range(length * 8):
        bit = (x[1] << 7) >> (i % 8)
        seq[i // 8] |= bit & 0xFF
        x0 = (x[8] + x[6] + x[4] + x[1]) % 2
        x[1], x[2], x[3], x[4], x[5], x[6], x[7], x[8] = (
            x[2],
            x[3],
            x[4],
            x[5],
            x[6],
            x[7],
            x[8],
            x0,
        )
    return bytes(seq)


def ccsds_xor_sequence(data: bytes) -> bytes:
    """XOR `data` against the CCSDS pseudo-random sequence (self-inverse)."""
    seq = ccsds_generate_sequence(len(data))
    return bytes(d ^ s for d, s in zip(data, seq, strict=True))
