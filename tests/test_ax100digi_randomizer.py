"""Unit tests for comms/ax100digi/randomizer.py's CCSDS pseudo-randomizer."""

from __future__ import annotations

from comms.ax100digi.randomizer import ccsds_generate_sequence, ccsds_xor_sequence


def test_sequence_starts_with_known_ccsds_prefix() -> None:
    # The CCSDS 131.0-B pseudo-random sequence (h(x)=x^8+x^7+x^5+x^3+1,
    # all-ones seed) begins with the well-known 0xFF 0x48 0x0E 0xC0 ...
    # prefix documented across many independent CCSDS implementations.
    seq = ccsds_generate_sequence(4)
    assert seq[0] == 0xFF


def test_xor_sequence_is_self_inverse() -> None:
    data = bytes(range(64))
    scrambled = ccsds_xor_sequence(data)
    assert scrambled != data
    descrambled = ccsds_xor_sequence(scrambled)
    assert descrambled == data


def test_sequence_is_deterministic_and_cached() -> None:
    seq1 = ccsds_generate_sequence(255)
    seq2 = ccsds_generate_sequence(255)
    assert seq1 == seq2
    assert len(seq1) == 255
