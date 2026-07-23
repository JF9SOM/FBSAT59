"""AX100 "ASM+Golay" digipeater/telemetry receiver (GreenCube/MARMOTSat-
compatible), fed by raw SDR I/Q.

Subscribes to an SDRPipeline's raw samples (pipeline.subscribe(), the same
tap used by aprs/afsk_demod.py and aprs/g3ruh_demod.py — *not*
pipeline.audio_ready, since the GMSK discriminator needs complex baseband,
not a generic voice-oriented demodulated audio stream), runs a continuous
GMSK discriminator, and buffers the result in a rolling window. A caller
(the UI tab, via a QTimer) periodically calls decode_pending() to batch-
process the buffer through the fixed-phase symbol slicer and AX100 frame
decoder — see gmsk_demod.py's module docstring for why a fixed-phase
slicer (rather than a continuously-tracking clock recovery loop) is
sufficient for these short frames.

Receive only — SDR hardware supported by this project cannot transmit; a
future TX path (Phase 2) will need Rig 1 + PTT, matching FT4/Q65's design.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from . import frame as frame_mod
from .csp import CspHeader, split_csp_packet
from .gmsk_demod import DEFAULT_BAUD, GmskDiscriminator, resample_to_sps, slice_bits_all_phases
from .message import DigiMessage, parse_message

# Rolling buffer length: comfortably longer than the longest possible AX100
# ASM+Golay frame (258 bytes @ 1200 baud =~ 1.7s) plus margin for a frame
# straddling two decode cycles.
_BUFFER_SECONDS = 3.0
_DEDUPE_SIZE = 32


@dataclass
class DecodedDigiFrame:
    """A decoded AX100 frame, with best-effort CSP/message interpretation."""

    payload: bytes  # raw CSP packet bytes (header + body)
    csp: CspHeader | None
    message: DigiMessage | None  # parsed store-and-forward message, if the body matched
    raw_text: str | None  # body as text/hex when it isn't a recognised digi message
    golay_bit_errors: int
    rs_bytes_corrected: int | None


class Ax100DigiReceiver:
    """Owns the GMSK discriminator + rolling buffer for one SDR pipeline."""

    def __init__(self, sample_rate: float, baud: float = DEFAULT_BAUD) -> None:
        self._discriminator = GmskDiscriminator(input_rate=sample_rate, baud=baud)
        self._sample_rate = sample_rate
        self._baud = baud
        self._lock = threading.Lock()
        self._buffer: list[NDArray[np.float32]] = []
        self._buffer_samples = 0
        self._max_buffer_samples = int(sample_rate * _BUFFER_SECONDS)
        self._recent_payloads: list[bytes] = []

    def push_samples(self, iq: NDArray[np.complex64]) -> None:
        """Feed raw I/Q. Safe to call from the SDR pipeline's own thread."""
        discrim = self._discriminator.process(iq)
        if len(discrim) == 0:
            return
        with self._lock:
            self._buffer.append(discrim)
            self._buffer_samples += len(discrim)
            while self._buffer_samples > self._max_buffer_samples and len(self._buffer) > 1:
                dropped = self._buffer.pop(0)
                self._buffer_samples -= len(dropped)

    def decode_pending(self) -> list[DecodedDigiFrame]:
        """Batch-decode the current rolling buffer, returning only frames
        not already reported in a previous call. Call periodically (e.g.
        every second) from the Qt main thread."""
        with self._lock:
            if not self._buffer:
                return []
            snapshot = np.concatenate(self._buffer)

        oversampled = resample_to_sps(snapshot, self._sample_rate, self._baud)
        candidates = slice_bits_all_phases(oversampled)

        new_frames: list[DecodedDigiFrame] = []
        for bits in candidates:
            for f in frame_mod.find_frames(bits):
                if f.payload in self._recent_payloads:
                    continue
                self._recent_payloads.append(f.payload)
                if len(self._recent_payloads) > _DEDUPE_SIZE:
                    self._recent_payloads.pop(0)
                new_frames.append(_interpret_frame(f))
        return new_frames


def _interpret_frame(f: frame_mod.Ax100Frame) -> DecodedDigiFrame:
    csp_header: CspHeader | None = None
    message: DigiMessage | None = None
    raw_text: str | None = None
    try:
        csp_header, body = split_csp_packet(f.payload)
    except ValueError:
        return DecodedDigiFrame(
            payload=f.payload,
            csp=None,
            message=None,
            raw_text=f.payload.hex(),
            golay_bit_errors=f.golay_bit_errors,
            rs_bytes_corrected=f.rs_bytes_corrected,
        )

    try:
        text = body.decode("ascii")
    except UnicodeDecodeError:
        raw_text = body.hex()
    else:
        message = parse_message(text)
        if message is None:
            raw_text = text

    return DecodedDigiFrame(
        payload=f.payload,
        csp=csp_header,
        message=message,
        raw_text=raw_text,
        golay_bit_errors=f.golay_bit_errors,
        rs_bytes_corrected=f.rs_bytes_corrected,
    )
