"""CCSDS Reed-Solomon (255,223) wrapper with libfec-style codeword shortening.

AX100 frames are usually shorter than a full 255-byte RS block, so the
transmitted codeword is a "shortened" RS(255,223) code: the missing leading
bytes are treated as implicit zero padding that is never sent over the air
(the same convention gr-satellites' lib/u482c_decode_impl.cc uses via
libfec's decode_rs_8(data, eras, no_eras, pad)).

Uses the reed-solomon-ccsds PyPI package (pure Python + numpy, no native
build required), which already implements the dual-basis <-> conventional
basis conversion CCSDS transmissions use on the wire.
"""

from __future__ import annotations

import numpy as np

try:
    import reed_solomon_ccsds as _rs
except ImportError:
    _rs = None

_BLOCK_LEN = 255
_PARITY_LEN = 32
_DATA_LEN = _BLOCK_LEN - _PARITY_LEN  # 223


class RsUnavailableError(RuntimeError):
    """Raised when the reed-solomon-ccsds package is not installed."""


class RsUncorrectableError(ValueError):
    """Raised when a shortened RS(255,223) codeword has too many byte errors."""


def is_available() -> bool:
    return _rs is not None


def decode_shortened(codeword: bytes, *, dual_basis: bool = True) -> tuple[int, bytes]:
    """Decode a shortened RS(255,223) codeword (message bytes + 32 parity bytes).

    `codeword` is `rx_len` bytes total (message + 32 parity), rx_len <= 255.
    Returns (num_corrected_bytes, message_bytes) with message_bytes length
    == len(codeword) - 32.
    """
    if _rs is None:
        raise RsUnavailableError("reed-solomon-ccsds is not installed")
    if len(codeword) < _PARITY_LEN or len(codeword) > _BLOCK_LEN:
        raise ValueError(f"shortened RS codeword must be {_PARITY_LEN}..{_BLOCK_LEN} bytes")

    pad = _BLOCK_LEN - len(codeword)
    padded = bytes(pad) + codeword
    try:
        count, decoded_data = _rs.decode_block(
            np.frombuffer(padded, dtype=np.uint8), dual_basis=dual_basis
        )
    except _rs.UncorrectableError as exc:
        raise RsUncorrectableError(str(exc)) from exc

    message = bytes(decoded_data)[pad:]
    return count, message


def encode_shortened(message: bytes, *, dual_basis: bool = True) -> bytes:
    """Encode `message` (<=223 bytes) into a shortened RS(255,223) codeword
    (message bytes followed by 32 parity bytes, no padding included)."""
    if _rs is None:
        raise RsUnavailableError("reed-solomon-ccsds is not installed")
    if len(message) > _DATA_LEN:
        raise ValueError(f"message must be at most {_DATA_LEN} bytes for RS(255,223)")

    pad = _DATA_LEN - len(message)
    padded = bytes(pad) + message
    encoded = _rs.encode_block(np.frombuffer(padded, dtype=np.uint8), dual_basis=dual_basis)
    encoded_bytes = bytes(encoded)
    # encode_block returns [data(223) + parity(32)]; drop the zero padding
    # from the data portion, keep parity in full.
    return encoded_bytes[pad:_DATA_LEN] + encoded_bytes[_DATA_LEN:]
