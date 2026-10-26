"""ERMINAZ-2 uplink audio: a TC frame -> baseband NRZ audio for the rig's FM data input.

Chain (DL7NDR's flowgraph): CRC-16 over the TC frame -> RS(255,223) conventional basis ->
CCSDS scrambler -> preamble (0101...) + ASM 0x1ACFFC1D in front, no Golay length field ->
GMSK. The rig's FM data input does the modulation, so this builds the Gaussian shaped (BT 0.5)
NRZ audio, like ``comms.foresail.tx``. Not checked on the air.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from comms.ax100digi import rs_ccsds
from comms.ax100digi.frame import SKYLINK_SYNCWORD, _bytes_to_bits, _syncword_bits
from comms.ax100digi.randomizer import ccsds_xor_sequence
from comms.erminaz.frames import DEFAULT_RADIO_CONF, build_digipeater_tc_frame
from comms.foresail.tx import AUDIO_RATE, gaussian_nrz_audio

# The CCSDS attached sync marker (the same 32 bits Skylink uses).
ASM = SKYLINK_SYNCWORD
DEFAULT_PREAMBLE_BITS = 128
DEFAULT_TAIL_BITS = 24


def uplink_bits(
    tc_frame: bytes,
    *,
    preamble_bits: int = DEFAULT_PREAMBLE_BITS,
    tail_bits: int = DEFAULT_TAIL_BITS,
) -> NDArray[np.uint8]:
    """The on-air bit sequence for *tc_frame* (preamble, ASM, scrambled RS codeword, tail)."""
    codeword = rs_ccsds.encode_shortened(tc_frame, dual_basis=False)
    scrambled = ccsds_xor_sequence(codeword)
    preamble = np.tile(np.array([0, 1], dtype=np.uint8), (preamble_bits + 1) // 2)[:preamble_bits]
    tail = np.tile(np.array([0, 1], dtype=np.uint8), (tail_bits + 1) // 2)[:tail_bits]
    return np.concatenate([preamble, _syncword_bits(ASM), _bytes_to_bits(scrambled), tail]).astype(
        np.uint8
    )


def build_uplink(
    scid: int,
    destination: str,
    source: str,
    message: str,
    *,
    radio_conf: int = DEFAULT_RADIO_CONF,
    sample_rate: int = AUDIO_RATE,
    preamble_bits: int = DEFAULT_PREAMBLE_BITS,
) -> tuple[bytes, NDArray[np.float32]]:
    """The TC frame bytes and the audio for a digipeater message."""
    frame = build_digipeater_tc_frame(scid, destination, source, message, radio_conf)
    bits = uplink_bits(frame, preamble_bits=preamble_bits)
    return frame, gaussian_nrz_audio(bits, sample_rate=sample_rate)
