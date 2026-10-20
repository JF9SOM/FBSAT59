"""Foresail-1p uplink waveform: an AX.25 frame for the repeater -> audio for the rig.

Chain (ICD section 2 / 4 and the Aalto ``repeater_frame_bytes.py`` / ``repeater_frame_wav.py``)::

    AX.25 frame (no flags, FCS MSB first)  ->  Skylink frame on VC 3 (no CRC, no extensions)
    ->  RS(255,223) conventional basis -> CCSDS scrambler -> Golay length -> ASM 0x1ACFFC1D
    ->  16 x 0xAA preamble in front  ->  NRZ, Gaussian shaped (BT 0.5)

The radio's FM data input does the FSK modulation, so the audio is a baseband NRZ waveform
(bit 1 = positive), like the G3RUH transmit audio but without the G3RUH scrambler. The bit
chain is the exact inverse of ``comms.foresail.rx`` (tests decode its output with it and with
gr-satellites); the pulse shape is ours, not Aalto's, and the level / polarity at the rig
still have to be confirmed on the air.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from comms.ax100digi.frame import SKYLINK_SYNCWORD, encode_frame
from comms.foresail.skylink import (
    SkylinkExtension,
    build_repeater_frame,
    build_skylink_frame,
)

BAUD = 9600
AUDIO_RATE = 48_000
DEFAULT_PREAMBLE_BYTES = 16
DEFAULT_TAIL_BITS = 24
GAUSSIAN_BT = 0.5
# RS(255,223) message length limit.
MAX_SKYLINK_FRAME_LEN = 223


def uplink_bits(
    skylink_frame: bytes,
    *,
    preamble_bytes: int = DEFAULT_PREAMBLE_BYTES,
    tail_bits: int = DEFAULT_TAIL_BITS,
) -> NDArray[np.uint8]:
    """The on-air bit sequence (preamble, sync, Golay length, scrambled RS codeword, tail)."""
    if len(skylink_frame) > MAX_SKYLINK_FRAME_LEN:
        raise ValueError(f"Skylink frame too long ({len(skylink_frame)} > {MAX_SKYLINK_FRAME_LEN})")
    frame_bits = encode_frame(
        skylink_frame,
        scrambler=True,
        rs=True,
        syncword=SKYLINK_SYNCWORD,
        golay_flags=False,
        rs_dual_basis=False,
    )
    preamble = np.unpackbits(np.full(preamble_bytes, 0xAA, dtype=np.uint8))
    tail = np.tile(np.array([0, 1], dtype=np.uint8), (tail_bits + 1) // 2)[:tail_bits]
    return np.concatenate([preamble, frame_bits, tail]).astype(np.uint8)


def gaussian_nrz_audio(
    bits: NDArray[np.uint8],
    *,
    sample_rate: int = AUDIO_RATE,
    baud: int = BAUD,
    bt: float = GAUSSIAN_BT,
) -> NDArray[np.float32]:
    """Gaussian-filtered NRZ baseband (-1..1, bit 1 = +1) at *sample_rate*."""
    sps = sample_rate / baud
    n = len(bits)
    # Rectangular NRZ at the audio rate (the sample count per bit follows the exact ratio).
    edges = np.round(np.arange(n + 1) * sps).astype(int)
    levels = np.repeat(bits.astype(np.float64) * 2.0 - 1.0, np.diff(edges))
    # Gaussian pulse: sigma = sqrt(ln 2) / (2 pi BT) symbols.
    sigma = np.sqrt(np.log(2.0)) / (2.0 * np.pi * bt) * sps
    half = int(np.ceil(3.0 * sigma)) + 1
    taps = np.exp(-0.5 * (np.arange(-half, half + 1) / sigma) ** 2)
    taps /= taps.sum()
    padded = np.concatenate([np.full(half, levels[0]), levels, np.full(half, levels[-1])])
    shaped = np.convolve(padded, taps, mode="valid")
    return np.asarray(shaped, dtype=np.float32)


def build_uplink(
    destination: str,
    source: str,
    info: bytes,
    digipeaters: tuple[str, ...] = (),
    *,
    identity: str = "PyGS",
    sequence: int = 0,
    extensions: tuple[SkylinkExtension, ...] = (),
    sample_rate: int = AUDIO_RATE,
    preamble_bytes: int = DEFAULT_PREAMBLE_BYTES,
) -> tuple[bytes, NDArray[np.float32]]:
    """The Skylink frame bytes and the audio for an AX.25 message to the repeater."""
    ax25 = build_repeater_frame(destination, source, info, digipeaters)
    skylink = build_skylink_frame(ax25, identity=identity, sequence=sequence, extensions=extensions)
    bits = uplink_bits(skylink, preamble_bytes=preamble_bytes)
    return skylink, gaussian_nrz_audio(bits, sample_rate=sample_rate)
