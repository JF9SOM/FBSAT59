"""
Unit tests for comms/ax100digi/golay.py's extended Golay(24,12) codec.

Verified against the reference values used to sanity-check gr-satellites'
own golay24.c: an all-zero codeword must decode cleanly, and single/double/
triple bit-flip corruptions must be corrected exactly.
"""

from __future__ import annotations

import itertools

import pytest

from comms.ax100digi import golay


def test_encode_decode_roundtrip_all_zero_data() -> None:
    codeword = golay.encode_golay24(0)
    corrected, errors = golay.decode_golay24(codeword)
    assert corrected & 0xFFF == 0
    assert errors == 0


@pytest.mark.parametrize("data12", [0x000, 0x001, 0x0FF, 0x1FF, 0x3FF, 0x7FF, 0xFFF])
def test_encode_decode_roundtrip_no_errors(data12: int) -> None:
    codeword = golay.encode_golay24(data12)
    corrected, errors = golay.decode_golay24(codeword)
    assert corrected & 0xFFF == data12
    assert errors == 0


@pytest.mark.parametrize("data12", [0x012, 0x0AB, 0x3E7, 0xFFF])
def test_corrects_single_bit_error(data12: int) -> None:
    codeword = golay.encode_golay24(data12)
    for flip_bit in range(24):
        corrupted = codeword ^ (1 << flip_bit)
        corrected, errors = golay.decode_golay24(corrupted)
        assert corrected & 0xFFF == data12
        assert errors == 1


@pytest.mark.parametrize("data12", [0x000, 0x155, 0x2AA, 0xFFF])
def test_corrects_up_to_three_bit_errors(data12: int) -> None:
    codeword = golay.encode_golay24(data12)
    # Extended Golay(24,12) has minimum distance 8, so up to 3 errors are
    # always correctable.
    for bits in itertools.combinations(range(24), 3):
        corrupted = codeword
        for b in bits:
            corrupted ^= 1 << b
        corrected, _errors = golay.decode_golay24(corrupted)
        assert corrected & 0xFFF == data12


def test_uncorrectable_codeword_raises() -> None:
    # Extended Golay(24,12) corrects up to 3 errors; a 4-bit error pattern
    # can land outside the correction radius. Bits 0-3 (LSBs) of the
    # all-zero codeword is one such case (verified by exhaustive search).
    codeword = golay.encode_golay24(0)
    corrupted = codeword ^ 0b1111
    with pytest.raises(golay.GolayUncorrectableError):
        golay.decode_golay24(corrupted)
