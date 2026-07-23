"""AX100 "ASM+Golay" (u482c, mode='ASM') frame codec.

Bit-exact reimplementation of gr-satellites' ax100_deframer(mode='ASM') +
u482c_decode_impl.cc (Daniel Estevez, GPL-3.0). This is the framing GreenCube
(IO-117) uses on 435.310 MHz, and per MARMOTSat's own documentation
("equipment requirements ... are the same as for Greencube") the VHF
digipeater on 145.875 MHz uses the identical protocol stack, just a
different band.

On-air frame layout (after GMSK demodulation + bit slicing, unpacked bits):

    [32-bit ASM sync word] [24-bit Golay(24,12) length field] [frame_len bytes]

The Golay-decoded 12-bit data field packs:
    bits 0-7   frame_len (0-255)
    bit  8     viterbi_flag
    bit  9     scrambler_flag  (CCSDS randomization)
    bit  10    rs_flag         (Reed-Solomon(255,223))
    bit  11    reserved

`frame_len` bytes follow. If rs_flag is set those are a *shortened*
RS(255,223) codeword (message + 32 parity bytes); the decoded message is the
CSP packet. Convolutional (Viterbi) coding is not implemented — GreenCube/
MARMOTSat's documented stack does not use it, so a frame with viterbi_flag
set is reported but not decoded further.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from . import golay, rs_ccsds
from .randomizer import ccsds_xor_sequence

# Default AX100 ASM sync word (gr-satellites' ax100_deframer default), unpacked
# bits, MSB-first per byte. Equal to hex C9D08A7B with each byte's bit order
# reversed (the AX100 modem's over-the-air bit order convention).
SYNCWORD: str = "10010011000010110101000111011110"
SYNCWORD_LEN: int = len(SYNCWORD)
DEFAULT_SYNC_THRESHOLD: int = 4

_GOLAY_FIELD_BITS = 24
_RS_PARITY_LEN = 32


class FrameDecodeError(ValueError):
    """Raised when a candidate frame cannot be decoded (caller should skip it)."""


@dataclass
class Ax100Frame:
    """A single decoded AX100 ASM+Golay frame."""

    payload: bytes  # CSP packet bytes (after descramble/RS, parity stripped)
    frame_len: int  # on-air byte count declared in the Golay length field
    golay_bit_errors: int
    viterbi_flag: bool
    scrambler_used: bool
    rs_used: bool
    rs_bytes_corrected: int | None  # None if RS was not applied
    bit_offset: int  # index in the input bit array where the sync word started


def _syncword_bits() -> NDArray[np.uint8]:
    return np.array([int(c) for c in SYNCWORD], dtype=np.uint8)


def _bits_to_bytes(bits: NDArray[np.uint8]) -> bytes:
    n = len(bits) // 8
    if n == 0:
        return b""
    packed = np.packbits(bits[: n * 8])
    return bytes(packed[:n])


def _bytes_to_bits(data: bytes) -> NDArray[np.uint8]:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


def find_frames(
    bits: NDArray[np.uint8], *, sync_threshold: int = DEFAULT_SYNC_THRESHOLD
) -> Iterator[Ax100Frame]:
    """Search an unpacked-bit array (values 0/1) for AX100 ASM+Golay frames.

    Yields frames in order of their sync-word position. Malformed
    candidates (bad Golay/RS, incomplete data) are silently skipped so the
    search can continue past false-positive sync matches.
    """
    sync = _syncword_bits()
    n = len(bits)
    i = 0
    while i <= n - SYNCWORD_LEN:
        window = bits[i : i + SYNCWORD_LEN]
        errors = int(np.count_nonzero(window != sync))
        if errors <= sync_threshold:
            frame = _try_decode_at(bits, i)
            if frame is not None:
                yield frame
                i += SYNCWORD_LEN + _GOLAY_FIELD_BITS + frame.frame_len * 8
                continue
        i += 1


def _try_decode_at(bits: NDArray[np.uint8], sync_start: int) -> Ax100Frame | None:
    golay_start = sync_start + SYNCWORD_LEN
    if golay_start + _GOLAY_FIELD_BITS > len(bits):
        return None

    golay_bits = bits[golay_start : golay_start + _GOLAY_FIELD_BITS]
    codeword = 0
    for bit in golay_bits:
        codeword = (codeword << 1) | int(bit)

    try:
        corrected, bit_errors = golay.decode_golay24(codeword)
    except golay.GolayUncorrectableError:
        return None

    data12 = corrected & 0xFFF
    frame_len = data12 & 0xFF
    viterbi_flag = bool(data12 & 0x100)
    scrambler_flag = bool(data12 & 0x200)
    rs_flag = bool(data12 & 0x400)

    if viterbi_flag:
        # Convolutional coding is not implemented (not used by GreenCube/
        # MARMOTSat's documented stack); report as undecodable.
        return None

    packet_start = golay_start + _GOLAY_FIELD_BITS
    packet_end = packet_start + frame_len * 8
    if packet_end > len(bits):
        return None

    packet = bytearray(_bits_to_bytes(bits[packet_start:packet_end]))
    rx_len = frame_len

    if scrambler_flag:
        packet[:rx_len] = ccsds_xor_sequence(bytes(packet[:rx_len]))

    rs_bytes_corrected: int | None = None
    if rs_flag:
        if rx_len < _RS_PARITY_LEN:
            return None
        try:
            rs_bytes_corrected, message = rs_ccsds.decode_shortened(bytes(packet[:rx_len]))
        except (rs_ccsds.RsUnavailableError, rs_ccsds.RsUncorrectableError):
            return None
        payload = message
    else:
        payload = bytes(packet[:rx_len])

    return Ax100Frame(
        payload=payload,
        frame_len=frame_len,
        golay_bit_errors=bit_errors,
        viterbi_flag=viterbi_flag,
        scrambler_used=scrambler_flag,
        rs_used=rs_flag,
        rs_bytes_corrected=rs_bytes_corrected,
        bit_offset=sync_start,
    )


def encode_frame(payload: bytes, *, scrambler: bool = True, rs: bool = True) -> NDArray[np.uint8]:
    """Build an on-air unpacked-bit array (sync word + Golay length + data)
    for `payload` (a CSP packet), for use by a future TX path.

    Convolutional coding is never applied (viterbi_flag is always 0).
    """
    packet = bytearray(payload)

    if rs:
        codeword = rs_ccsds.encode_shortened(bytes(packet))
        packet = bytearray(codeword)

    if scrambler:
        packet[:] = ccsds_xor_sequence(bytes(packet))

    frame_len = len(packet)
    if frame_len > 255:
        raise ValueError(f"encoded frame too long ({frame_len} bytes, max 255)")

    data12 = (frame_len & 0xFF) | (0x200 if scrambler else 0) | (0x400 if rs else 0)
    codeword24 = golay.encode_golay24(data12)

    golay_bits = np.array([(codeword24 >> (23 - i)) & 1 for i in range(24)], dtype=np.uint8)
    packet_bits = _bytes_to_bits(bytes(packet))

    return np.concatenate([_syncword_bits(), golay_bits, packet_bits])
