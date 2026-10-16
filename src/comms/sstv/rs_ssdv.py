"""Reed-Solomon (255, 223) errors-and-erasures decoder for SSDV packets.

SSDV (type 0x66) packets carry 32 bytes of CCSDS Reed-Solomon parity. The stock ``ssdv`` tool
repairs a packet while it builds the image but does not say which packets it repaired, and the
Stored Images feature must keep only packets that are known to be good. This module repairs a
packet in the app; the caller checks the CRC-32 afterwards.

Code parameters (the CCSDS code as used by SSDV, found by checking the parity of packets
received from HADES-SA): GF(2^8) with field polynomial 0x187, first consecutive root 112,
primitive element ``alpha ** 11``, 32 parity bytes. The codeword is ``packet[1:256]``
(type byte .. parity), i.e. a full-length 255-byte code word; shorter packets (``ssdv -l``)
are a shortened code, padded here with leading zeros.

Erasures (known-bad positions) are supported: ``e`` erasures plus ``t`` unknown errors are
fixed whenever ``e + 2t <= 32``. Only CRC-32-verified output should be trusted: with many
errors a wrong decode is possible.
"""

from __future__ import annotations

_FIELD_POLY = 0x187
_FCR = 112
_PRIM = 11
NROOTS = 32
N = 255


def _build_tables() -> tuple[list[int], list[int]]:
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= _FIELD_POLY
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_EXP, _LOG = _build_tables()


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _inv(a: int) -> int:
    return _EXP[255 - _LOG[a]]


def _root(i: int) -> int:
    """The i-th generator root, ``alpha ** (PRIM * (FCR + i))``."""
    return _EXP[(_PRIM * (_FCR + i)) % 255]


def _generator() -> list[int]:
    """Generator polynomial coefficients, highest degree first."""
    g = [1]
    for i in range(NROOTS):
        r = _root(i)
        nxt = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            nxt[j] ^= c
            nxt[j + 1] ^= _mul(c, r)
        g = nxt
    return g


_GEN = _generator()


def encode(data: bytes) -> bytes:
    """The 32 parity bytes of a 223-byte message (used by the tests)."""
    if len(data) != N - NROOTS:
        raise ValueError("message must be 223 bytes")
    rem = [0] * NROOTS
    for byte in data:
        fb = byte ^ rem[0]
        rem = [*rem[1:], 0]
        if fb:
            for j in range(NROOTS):
                rem[j] ^= _mul(fb, _GEN[j + 1])
    return bytes(rem)


def syndromes(word: bytes) -> list[int]:
    """The 32 syndromes of a 255-byte code word (all zero for a valid one)."""
    out = []
    for i in range(NROOTS):
        r = _root(i)
        s = 0
        for byte in word:
            s = _mul(s, r) ^ byte
        out.append(s)
    return out


def decode(word: bytes, erasures: list[int] | None = None) -> bytes | None:
    """Correct a 255-byte code word. Returns the corrected word, or None if not decodable.

    *erasures* are byte positions (0..254) known to be unreliable.
    """
    if len(word) != N:
        return None
    erasure_list = sorted(set(erasures or []))
    if len(erasure_list) > NROOTS:
        return None
    syn = syndromes(word)
    if not any(syn):
        return bytes(word)

    def locator(pos: int) -> int:
        return _EXP[(_PRIM * (N - 1 - pos)) % 255]

    # erasure locator polynomial, lowest degree first
    lam = [1]
    for pos in erasure_list:
        x = locator(pos)
        nxt = [*lam, 0]
        for j, c in enumerate(lam):
            nxt[j + 1] ^= _mul(c, x)
        lam = nxt
    e = len(erasure_list)
    b = list(lam)
    deg = e
    for r in range(e, NROOTS):
        delta = 0
        for j in range(min(deg, r) + 1):
            if j < len(lam):
                delta ^= _mul(lam[j], syn[r - j])
        shifted = [0, *b]
        if delta:
            size = max(len(lam), len(shifted))
            new = [0] * size
            for j in range(size):
                new[j] = (lam[j] if j < len(lam) else 0) ^ _mul(
                    delta, shifted[j] if j < len(shifted) else 0
                )
            if 2 * deg <= r + e:
                dinv = _inv(delta)
                b = [_mul(dinv, c) for c in lam]
                deg = r + 1 + e - deg
            else:
                b = shifted
            lam = new
        else:
            b = shifted
    while len(lam) > 1 and lam[-1] == 0:
        lam.pop()
    if len(lam) - 1 != deg or deg > NROOTS:
        return None

    # roots of the locator (Chien search): positions whose inverse locator is a root
    positions: list[int] = []
    for pos in range(N):
        xinv = _EXP[(255 - (_PRIM * (N - 1 - pos)) % 255) % 255]
        val = 0
        xp = 1
        for c in lam:
            val ^= _mul(c, xp)
            xp = _mul(xp, xinv)
        if val == 0:
            positions.append(pos)
    if len(positions) != deg:
        return None

    # error evaluator Omega = S(x) * Lambda(x) mod x^NROOTS
    omega = [0] * NROOTS
    for i in range(NROOTS):
        acc = 0
        for j in range(min(i, len(lam) - 1) + 1):
            acc ^= _mul(lam[j], syn[i - j])
        omega[i] = acc
    out = bytearray(word)
    for pos in positions:
        x = locator(pos)
        xinv = _inv(x)
        om = 0
        xp = 1
        for c in omega:
            om ^= _mul(c, xp)
            xp = _mul(xp, xinv)
        # formal derivative: odd-degree terms only
        der = 0
        for j in range(1, len(lam), 2):
            xp = _EXP[(_LOG[xinv] * (j - 1)) % 255] if j > 1 else 1
            der ^= _mul(lam[j], xp)
        if der == 0:
            return None
        mag = _mul(_mul(om, _inv(der)), _EXP[(_LOG[x] * (1 - _FCR)) % 255])
        out[pos] ^= mag
    if any(syndromes(bytes(out))):
        return None
    return bytes(out)
