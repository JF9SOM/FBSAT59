"""Build a transmittable GMSK/SSB audio waveform for a GreenCube/MARMOTSat
digipeater store-and-forward message.

Pipeline: build_message() text -> CSP packet -> frame.encode_frame() bits
-> leading preamble + trailing tail bits -> audio_bridge.synthesize_gmsk_audio().

CSP header field values (source/destination node, ports) are **not
confirmed against a real MARMOTSat/GreenCube ground station** — the
GreenCube Digipeater Manual documents the application-layer message text
format precisely, but does not publish the CSP addressing its own
Terminal software uses. The defaults below are placeholders; real-world
transmission may need these adjusted once the correct values are known
(see CLAUDE.md's Phase 0/1 plan). They are exposed as parameters so the
caller (the UI) can make them user-configurable rather than silently
guessing.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .audio_bridge import DEFAULT_SHIFT_HZ, synthesize_gmsk_audio
from .csp import CspHeader, build_csp_packet
from .frame import encode_frame
from .gmsk_demod import DEFAULT_BAUD, DEFAULT_DEVIATION_HZ
from .message import build_message

# Bit counts, not audio samples. A leading run-in lets the receiver's IF
# filters settle before the sync word arrives; a trailing tail gives the
# resampler margin at the end of the buffer (see
# test_ax100digi_audio_bridge.py's discovery that zero tail margin can
# lose the last symbol or two to resample_to_sps()'s transient).
DEFAULT_PREAMBLE_SYMBOLS = 100
DEFAULT_TAIL_SYMBOLS = 40

# Placeholder CSP addressing — NOT verified against a real satellite (see
# module docstring).
DEFAULT_CSP_HEADER = CspHeader(priority=1, source=1, destination=5, dest_port=10, source_port=20)


@dataclass
class TxAudioResult:
    audio: NDArray[np.float32]
    message_text: str
    payload: bytes


def build_tx_audio(
    my_call: str,
    dest_call: str,
    sat_name: str,
    content: str,
    *,
    store_seconds: int = 0,
    csp_header: CspHeader = DEFAULT_CSP_HEADER,
    sample_rate: float = 48_000.0,
    baud: float = DEFAULT_BAUD,
    deviation_hz: float = DEFAULT_DEVIATION_HZ,
    shift_hz: float = DEFAULT_SHIFT_HZ,
    scrambler: bool = True,
    rs: bool = True,
) -> TxAudioResult:
    """Build the full GMSK-over-SSB audio waveform for one outgoing
    digipeater message, ready to play through a Rig + Sound Card output
    with the radio in SSB mode."""
    message_text = build_message(my_call, dest_call, sat_name, content, store_seconds)
    payload = build_csp_packet(csp_header, message_text.encode("ascii"))

    frame_bits = encode_frame(payload, scrambler=scrambler, rs=rs)
    preamble = np.tile(np.array([0, 1], dtype=np.uint8), DEFAULT_PREAMBLE_SYMBOLS // 2)
    tail = np.tile(np.array([0, 1], dtype=np.uint8), DEFAULT_TAIL_SYMBOLS // 2)
    full_bits = np.concatenate([preamble, frame_bits, tail])

    audio = synthesize_gmsk_audio(
        full_bits,
        sample_rate=sample_rate,
        baud=baud,
        deviation_hz=deviation_hz,
        shift_hz=shift_hz,
    )
    return TxAudioResult(audio=audio, message_text=message_text, payload=payload)
