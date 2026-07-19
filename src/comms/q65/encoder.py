"""Q65 TX encoder — pure Python implementation.

Converts a message text string to 85 FSK tone indices and then to PCM audio,
following the algorithm in WSJT-X lib/qra/q65/ (genq65.f90 +
q65_encoding_modules.f90 by K1JT / kgoba et al., GPL-2.0).

Pipeline:
  pack77(text)                -> 77-bit FT8 payload  (via ft8_lib)
  get_q65crc12(mbits)         -> 90-bit array (77 msg + 1 zero + 12 CRC)
  bits_to_gf64_symbols(mbits) -> 15 GF(64) message symbols
  q65_linear_encode(message)  -> 65-symbol systematic codeword
  shorten(codeword)           -> 63-symbol shortcodeword (drops CRC symbols 14-15)
  insert_sync(shortcodeword)  -> 85 tone indices (0-64)
  synthesize_audio(tones, ...) -> float32 PCM at 12 000 Hz

TX requires ft8_lib for the pack77 step (same dependency as FT4).
If ft8_lib is unavailable, get_q65_tones() raises RuntimeError.
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

# ---------------------------------------------------------------------------
# Physical-layer constants
# ---------------------------------------------------------------------------

SAMPLE_RATE: int = 12_000  # Hz (must match Q65 spec)

# nsps (samples per symbol) for each period length (seconds)
_NSPS: dict[int, int] = {15: 1800, 30: 3600, 60: 7200}

# Tone-spacing multiplier for submode letter (A=1, B=2, C=4, D=8, E=16)
_SUBMODE_MULT: dict[str, int] = {"A": 1, "B": 2, "C": 4, "D": 8, "E": 16}

# Sync positions (0-indexed out of 85 symbols)
# From genq65.f90: isync=[1,9,12,13,15,22,23,26,27,33,35,38,46,50,55,60,62,66,69,74,76,85]
_SYNC_IDX: frozenset[int] = frozenset(
    [0, 8, 11, 12, 14, 21, 22, 25, 26, 32, 34, 37, 45, 49, 54, 59, 61, 65, 68, 73, 75, 84]
)

# ---------------------------------------------------------------------------
# GF(64) arithmetic — primitive polynomial x^6+x+1
# Log / antilog tables from q65_encoding_modules.f90
# ---------------------------------------------------------------------------

_GF64_LOG: list[int] = [
    -1,
    0,
    1,
    6,
    2,
    12,
    7,
    26,
    3,
    32,
    13,
    35,
    8,
    48,
    27,
    18,
    4,
    24,
    33,
    16,
    14,
    52,
    36,
    54,
    9,
    45,
    49,
    38,
    28,
    41,
    19,
    56,
    5,
    62,
    25,
    11,
    34,
    31,
    17,
    47,
    15,
    23,
    53,
    51,
    37,
    44,
    55,
    40,
    10,
    61,
    46,
    30,
    50,
    22,
    39,
    43,
    29,
    60,
    42,
    21,
    20,
    59,
    57,
    58,
]

_GF64_ANTILOG: list[int] = [
    1,
    2,
    4,
    8,
    16,
    32,
    3,
    6,
    12,
    24,
    48,
    35,
    5,
    10,
    20,
    40,
    19,
    38,
    15,
    30,
    60,
    59,
    53,
    41,
    17,
    34,
    7,
    14,
    28,
    56,
    51,
    37,
    9,
    18,
    36,
    11,
    22,
    44,
    27,
    54,
    47,
    29,
    58,
    55,
    45,
    25,
    50,
    39,
    13,
    26,
    52,
    43,
    21,
    42,
    23,
    46,
    31,
    62,
    63,
    61,
    57,
    49,
    33,
]


def _gf64_add(a: int, b: int) -> int:
    return (a ^ b) & 63


def _gf64_mult(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF64_ANTILOG[(_GF64_LOG[a] + _GF64_LOG[b]) % 63]


# ---------------------------------------------------------------------------
# Generator matrix — 15 rows x 50 cols over GF(64)
# From q65_encoding_modules.f90 `data generator/...` (column-major Fortran).
# Each inner list is one column (parity position j=0..49).
# G[row_i][col_j] = Fortran generator(row_i+1, col_j+1)
# ---------------------------------------------------------------------------

_G_COLS: list[list[int]] = [
    [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0],
    [0, 20, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0],
    [0, 20, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0],
    [0, 20, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0],
    [0, 20, 0, 1, 1, 0, 0, 0, 10, 0, 0, 0, 0, 1, 0],
    [0, 20, 0, 1, 1, 0, 0, 0, 10, 0, 0, 0, 44, 1, 0],
    [0, 20, 0, 1, 1, 0, 0, 0, 10, 1, 0, 0, 44, 1, 0],
    [0, 20, 0, 1, 1, 0, 0, 0, 10, 1, 0, 0, 44, 1, 14],
    [0, 20, 0, 1, 1, 0, 0, 0, 10, 1, 31, 0, 44, 1, 14],
    [0, 20, 0, 1, 1, 33, 0, 0, 10, 1, 31, 0, 44, 1, 14],
    [56, 20, 0, 1, 1, 33, 0, 0, 10, 1, 31, 0, 44, 1, 14],
    [56, 20, 0, 1, 1, 33, 0, 1, 10, 1, 31, 0, 44, 1, 14],
    [56, 1, 0, 1, 1, 33, 0, 1, 10, 1, 31, 0, 44, 1, 14],
    [56, 1, 0, 1, 1, 33, 0, 1, 10, 1, 31, 36, 44, 1, 14],
    [56, 1, 0, 1, 1, 33, 0, 1, 43, 1, 31, 36, 44, 1, 14],
    [56, 1, 0, 1, 1, 33, 0, 1, 43, 17, 31, 36, 44, 1, 14],
    [56, 1, 0, 1, 1, 33, 0, 1, 43, 17, 31, 36, 36, 1, 14],
    [56, 1, 0, 1, 1, 33, 53, 1, 43, 17, 31, 36, 36, 1, 14],
    [56, 1, 0, 35, 1, 33, 53, 1, 43, 17, 31, 36, 36, 1, 14],
    [56, 1, 0, 35, 1, 33, 53, 1, 43, 17, 30, 36, 36, 1, 14],
    [56, 1, 0, 35, 1, 33, 53, 52, 43, 17, 30, 36, 36, 1, 14],
    [56, 1, 0, 35, 1, 32, 53, 52, 43, 17, 30, 36, 36, 1, 14],
    [56, 1, 60, 35, 1, 32, 53, 52, 43, 17, 30, 36, 36, 1, 14],
    [56, 1, 60, 35, 1, 32, 53, 52, 43, 17, 30, 36, 36, 49, 14],
    [56, 1, 60, 35, 1, 32, 53, 52, 43, 17, 30, 36, 37, 49, 14],
    [56, 1, 60, 35, 54, 32, 53, 52, 43, 17, 30, 36, 37, 49, 14],
    [56, 1, 60, 35, 54, 32, 53, 52, 1, 17, 30, 36, 37, 49, 14],
    [1, 1, 60, 35, 54, 32, 53, 52, 1, 17, 30, 36, 37, 49, 14],
    [1, 0, 60, 35, 54, 32, 53, 52, 1, 17, 30, 36, 37, 49, 14],
    [1, 0, 60, 35, 54, 32, 53, 52, 1, 17, 30, 37, 37, 49, 14],
    [1, 0, 61, 35, 54, 32, 53, 52, 1, 17, 30, 37, 37, 49, 14],
    [1, 0, 61, 35, 54, 32, 53, 52, 1, 48, 30, 37, 37, 49, 14],
    [1, 0, 61, 35, 54, 32, 53, 52, 1, 48, 30, 37, 37, 49, 15],
    [1, 0, 61, 35, 54, 0, 53, 52, 1, 48, 30, 37, 37, 49, 15],
    [1, 0, 61, 35, 54, 0, 52, 52, 1, 48, 30, 37, 37, 49, 15],
    [1, 0, 61, 35, 54, 0, 52, 52, 1, 48, 30, 37, 37, 0, 15],
    [1, 0, 61, 35, 54, 0, 52, 34, 1, 48, 30, 37, 37, 0, 15],
    [1, 0, 61, 35, 54, 0, 52, 34, 1, 48, 30, 37, 0, 0, 15],
    [1, 0, 61, 35, 54, 0, 52, 34, 1, 48, 30, 20, 0, 0, 15],
    [1, 0, 0, 35, 54, 0, 52, 34, 1, 48, 30, 20, 0, 0, 15],
    [1, 0, 0, 35, 54, 0, 52, 34, 1, 0, 30, 20, 0, 0, 15],
    [0, 0, 0, 35, 54, 0, 52, 34, 1, 0, 30, 20, 0, 0, 15],
    [0, 0, 0, 35, 54, 0, 52, 34, 1, 0, 38, 20, 0, 0, 15],
    [0, 0, 0, 35, 0, 0, 52, 34, 1, 0, 38, 20, 0, 0, 15],
    [0, 0, 0, 35, 0, 0, 52, 0, 1, 0, 38, 20, 0, 0, 15],
    [0, 0, 0, 35, 0, 0, 52, 0, 1, 0, 38, 20, 0, 0, 0],
    [0, 0, 0, 35, 0, 0, 52, 0, 0, 0, 38, 20, 0, 0, 0],
    [0, 0, 0, 35, 0, 0, 52, 0, 0, 0, 38, 0, 0, 0, 0],
    [0, 0, 0, 0, 0, 0, 52, 0, 0, 0, 38, 0, 0, 0, 0],
    [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 38, 0, 0, 0, 0],
]  # 50 columns x 15 rows


# Build row-major G[15][50] by transposing _G_COLS
_G: list[list[int]] = [[_G_COLS[j][i] for j in range(50)] for i in range(15)]

# ---------------------------------------------------------------------------
# pack77 — 77-bit FT8 message packing via ft8_lib
# ---------------------------------------------------------------------------


def pack77(text: str) -> list[int]:
    """Return 77 bits (list of int 0/1) for the given FT8/Q65 message text.

    Uses ft8_lib (kgoba/ft8_lib) for encoding.  Raises RuntimeError if
    ft8_lib is not available or the message text is invalid.
    """
    try:
        import ctypes

        from comms.ft4.codec import _find_ft8lib, _FtxMessage, free_ft8lib

        lib = _find_ft8lib()
        if lib is None:
            raise RuntimeError("ft8lib not installed — Q65 TX requires ft8lib")

        try:
            msg = _FtxMessage()
            rc = lib.ftx_message_encode(ctypes.byref(msg), None, text.upper().encode("ascii"))
            if rc != 0:
                raise RuntimeError(f"ftx_message_encode returned {rc} for: {text!r}")

            # Extract first 77 bits from the 10-byte big-endian payload
            bits: list[int] = []
            for byte in bytes(msg.payload):
                for shift in range(7, -1, -1):
                    bits.append((byte >> shift) & 1)
            return bits[:77]
        finally:
            free_ft8lib(lib)

    except ImportError as exc:
        raise RuntimeError("ft4.codec not available — cannot use ft8lib pack77") from exc


# ---------------------------------------------------------------------------
# CRC-12 — polynomial [1,1,0,0,0,0,0,0,0,1,1,1,1] (13 coefficients)
# Ports get_q65crc12() from q65_encoding_modules.f90
# ---------------------------------------------------------------------------

_CRC12_POLY: list[int] = [1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1]


def _get_q65crc12(mbits: list[int]) -> list[int]:
    """Append 12-bit CRC to the 77-bit message, returning a 90-bit list.

    Input mbits is 77 bits.  The returned list has 90 elements:
    [0:77] = original message, [77] = 0 (pad), [78:90] = 12-bit CRC.
    """
    mc2 = list(mbits) + [0] * (90 - len(mbits))  # length 90

    # Build mc: bit-reverse each 6-bit symbol
    mc = [0] * 90
    for i in range(15):
        tmp = mc2[i * 6 : i * 6 + 6]
        mc[i * 6 : i * 6 + 6] = tmp[::-1]

    # LFSR — 78 clock cycles
    r = mc[:13]
    for i in range(78):
        r[12] = mc[i + 12]
        new_r = [(r[k] + r[0] * _CRC12_POLY[k]) % 2 for k in range(13)]
        r = new_r[1:] + [new_r[0]]  # cshift left by 1

    # Store CRC bits: r(6:1:-1) → mc2[78:84], r(12:7:-1) → mc2[84:90]
    mc2[78:84] = r[5::-1]
    mc2[84:90] = r[11:5:-1]

    return mc2


# ---------------------------------------------------------------------------
# GF(64) linear encoding — (65,15) systematic code
# ---------------------------------------------------------------------------


def _q65_linear_encode(message: list[int]) -> list[int]:
    """Encode 15 GF(64) message symbols to a 65-symbol systematic codeword.

    codeword[0:15] = message (systematic)
    codeword[15:65] = parity via 15x50 generator matrix
    """
    codeword = list(message) + [0] * 50
    for i in range(15):
        if message[i] == 0:
            continue
        for j in range(50):
            codeword[j + 15] = _gf64_add(codeword[j + 15], _gf64_mult(message[i], _G[i][j]))
    return codeword


# ---------------------------------------------------------------------------
# Full Q65 tone-index pipeline
# ---------------------------------------------------------------------------


def get_q65_tones(text: str) -> list[int]:
    """Full Q65 encoding: message text -> 85 tone indices (0-64).

    Corresponds to get_q65_tones() in q65_encoding_modules.f90.
    Raises RuntimeError if ft8lib is missing or the message is invalid.
    """
    # 1. Pack to 77 bits
    mbits = pack77(text)

    # 2. Compute CRC-12 and extend to 90 bits
    mbits_90 = _get_q65crc12(mbits)

    # 3. Group 90 bits into 15 six-bit GF(64) symbols (MSB first)
    message = [0] * 15
    for i in range(15):
        val = 0
        for bit in mbits_90[i * 6 : i * 6 + 6]:
            val = (val << 1) | bit
        message[i] = val

    # 4. Systematic (65,15) linear encode
    codeword = _q65_linear_encode(message)

    # 5. Shorten: drop CRC symbols at positions 13 and 14 (0-indexed)
    #    shortcw[0:13]  = codeword[0:13]
    #    shortcw[13:63] = codeword[15:65]
    shortcw = codeword[:13] + codeword[15:65]  # 63 symbols

    # 6. Insert 22 sync symbols (tone=0) at fixed positions
    itone = [0] * 85
    data_k = 0
    for pos in range(85):
        if pos in _SYNC_IDX:
            itone[pos] = 0
        else:
            itone[pos] = shortcw[data_k] + 1
            data_k += 1

    return itone


# ---------------------------------------------------------------------------
# 65-FSK audio synthesis
# ---------------------------------------------------------------------------


def synthesize_audio(
    tones: list[int],
    period_seconds: int,
    submode: str,
    f0: float = 1500.0,
) -> NDArray[np.float32]:
    """Convert 85 Q65 tone indices to float32 PCM at 12 000 Hz.

    Args:
        tones:          85 tone indices (0-64) from get_q65_tones().
        period_seconds: Q65 period (15, 30, or 60 s).
        submode:        "A"-"E" — controls tone spacing multiplier.
        f0:             Base frequency in Hz (default 1500 Hz).

    Returns:
        float32 PCM array of length 85 * nsps.
    """
    nsps = _NSPS.get(period_seconds, 7200)
    tone_mult = _SUBMODE_MULT.get(submode.upper(), 1)
    baud = SAMPLE_RATE / nsps
    df = baud * tone_mult  # Hz per tone step

    total = 85 * nsps
    audio = np.zeros(total, dtype=np.float32)

    # Build per-symbol taper (4th-power half-cosine over 3% of nsps)
    taper_len = max(1, nsps // 32)
    window = np.ones(nsps, dtype=np.float32)
    ramp = np.arange(taper_len, dtype=np.float32) / taper_len
    cos_ramp = (np.cos(math.pi * (1.0 - ramp)) * 0.5 + 0.5).astype(np.float32) ** 4
    window[:taper_len] = cos_ramp
    window[nsps - taper_len :] = cos_ramp[::-1]

    twopi = 2.0 * math.pi
    phase = 0.0

    for sym_idx, tone in enumerate(tones):
        freq = f0 + tone * df
        angular = twopi * freq / SAMPLE_RATE
        start = sym_idx * nsps
        k_arr = np.arange(nsps, dtype=np.float64)
        sym_audio = np.cos(phase + k_arr * angular).astype(np.float32) * window
        audio[start : start + nsps] = sym_audio
        phase = (phase + nsps * angular) % twopi

    peak = float(np.max(np.abs(audio)))
    if peak > 0.0:
        audio /= peak

    return audio
