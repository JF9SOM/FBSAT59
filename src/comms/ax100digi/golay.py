"""Extended Golay(24,12) codec for the AX100 "ASM+Golay" length field.

Bit-exact port of gr-satellites' lib/golay24.c (Daniel Estevez, GPL-3.0),
itself based on R.H. Morelos-Zaragoza, "The Art of Error Correcting
Coding", Wiley 2002, Section 2.2.3. Used by GOMspace AX100 (and therefore
GreenCube/MARMOTSat) to protect the 12-bit length field that precedes each
Reed-Solomon-coded frame.
"""

from __future__ import annotations

_N = 12

# Parity-check matrix rows (12 x 12-bit codeword positions), taken verbatim
# from golay24.c's H[] table.
_H: tuple[int, ...] = (
    0x8008ED,
    0x4001DB,
    0x2003B5,
    0x100769,
    0x80ED1,
    0x40DA3,
    0x20B47,
    0x1068F,
    0x8D1D,
    0x4A3B,
    0x2477,
    0x1FFE,
)


def _parity(x: int) -> int:
    return bin(x).count("1") & 1


def _b(i: int) -> int:
    return _H[i] & 0xFFF


class GolayUncorrectableError(ValueError):
    """Raised when a Golay(24,12) codeword has more errors than can be corrected."""


def encode_golay24(data12: int) -> int:
    """Encode 12 data bits into a 24-bit systematic extended Golay codeword."""
    r = data12 & 0xFFF
    s = 0
    for i in range(_N):
        s = (s << 1) | _parity(_H[i] & r)
    return ((s & 0xFFF) << _N) | r


def decode_golay24(codeword: int) -> tuple[int, int]:
    """Decode a 24-bit codeword, correcting up to 3 bit errors.

    Returns (corrected_24bit_codeword, num_bits_corrected). The low 12 bits
    of the corrected codeword are the original data field.
    """
    r = codeword & 0xFFFFFF
    s = 0
    for i in range(_N):
        s = (s << 1) | _parity(_H[i] & r)
    s &= 0xFFF

    if bin(s).count("1") <= 3:
        e = s << _N
        return r ^ e, bin(e).count("1")

    for i in range(_N):
        v = s ^ _b(i)
        if bin(v).count("1") <= 2:
            e = (v << _N) | (1 << (_N - i - 1))
            return r ^ e, bin(e).count("1")

    q = 0
    for i in range(_N):
        q = (q << 1) | _parity(_b(i) & s)
    q &= 0xFFF

    if bin(q).count("1") <= 3:
        return r ^ q, bin(q).count("1")

    for i in range(_N):
        v = q ^ _b(i)
        if bin(v).count("1") <= 2:
            e = (1 << (2 * _N - i - 1)) | v
            return r ^ e, bin(e).count("1")

    raise GolayUncorrectableError("Golay(24,12) codeword has too many errors to correct")
