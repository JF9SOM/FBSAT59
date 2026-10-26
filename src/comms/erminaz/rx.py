"""ERMINAZ-2 receiver: the FM discriminator output -> 223 octet TM frames.

The downlink is 9600 baud GMSK carrying a preamble, the CCSDS ASM ``0x1ACFFC1D`` and one
CCSDS-scrambled conventional-basis RS(255,223) codeword of fixed length (no length field),
i.e. what gr-satellites' ``CCSDS Reed-Solomon`` deframer reads. The modulation index is not
known to be 0.5, so the receiver does not use the coherent MSK detector: the SDR's I/Q is FM
demodulated (to 48 kHz audio, like the rig's data output) and both inputs share the baseband
slicer of ``comms.foresail.rx`` (8 timing phases, several low-pass cut-offs, both polarities).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from numpy.typing import NDArray

from comms.ax100digi import rs_ccsds
from comms.ax100digi.frame import SKYLINK_SYNCWORD, _bits_to_bytes, _syncword_bits
from comms.ax100digi.randomizer import ccsds_xor_sequence
from comms.foresail import rx as _fs
from comms.foresail.rx import (
    AUDIO_RATE,
    BAUD,
    AudioBitSource,
    ForesailRxFrame,
    ForesailRxStream,
    ForesailSdrDemod,
)

try:
    from scipy import signal as sp_signal

    _SCIPY_AVAILABLE: bool = True
except ImportError:
    sp_signal = None
    _SCIPY_AVAILABLE = False

FRAME_BYTES = 223
_CODEWORD_BYTES = 255
_SYNC = _syncword_bits(SKYLINK_SYNCWORD)
_SYNC_LEN = len(_SYNC)
_SYNC_ERRORS = 4  # the gr-satellites "syncword threshold" DL7NDR's definition uses
_DEDUP_S = 0.1

# Re-exported so callers import everything from this module.
ErminazRxFrame = ForesailRxFrame


class FmBitSource:
    """Raw bits from I/Q: channel filter + decimate to 48 kHz, FM discriminator, then the slicer."""

    def __init__(self, sample_rate: float) -> None:
        self.sample_rate = float(sample_rate)
        self._audio = AudioBitSource(AUDIO_RATE)

    def signal_present(self, iq: NDArray[Any]) -> bool:
        return len(iq) >= 2048 and float(np.max(np.abs(iq))) > 1e-6

    def discriminate(self, iq: NDArray[Any]) -> NDArray[np.float32]:
        """FM-demodulated audio (48 kHz) of *iq*."""
        x = np.asarray(iq, dtype=np.complex64)
        rate = int(round(self.sample_rate))
        divisor = math.gcd(rate, AUDIO_RATE)
        narrow = sp_signal.resample_poly(x, AUDIO_RATE // divisor, rate // divisor)
        phase = np.angle(narrow[1:] * np.conj(narrow[:-1]))
        return np.asarray(phase, dtype=np.float32)

    def raw_bit_streams(self, iq: NDArray[Any]) -> list[tuple[int, NDArray[np.uint8]]]:
        if not _SCIPY_AVAILABLE or len(iq) < 2048:
            return []
        return self._audio.raw_bit_streams(self.discriminate(iq))


def decode_frame_at(bits: NDArray[np.uint8], sync_start: int) -> tuple[bytes, int] | None:
    """The 223 octet frame whose sync word starts at *sync_start*, and the corrected bytes."""
    start = sync_start + _SYNC_LEN
    end = start + _CODEWORD_BYTES * 8
    if end > len(bits):
        return None
    codeword = ccsds_xor_sequence(_bits_to_bytes(bits[start:end]))
    try:
        corrected, message = rs_ccsds.decode_shortened(codeword, dual_basis=False)
    except (rs_ccsds.RsUnavailableError, rs_ccsds.RsUncorrectableError, ValueError):
        return None
    return message, corrected


class ErminazRxStream(ForesailRxStream):
    """The Foresail stream (windowing, de-duplication) with the ERMINAZ-2 framing.

    ``audio=True`` takes 48 kHz discriminator audio; otherwise I/Q at *sample_rate*.
    """

    def __init__(self, sample_rate: float, *, audio: bool = False) -> None:
        super().__init__(sample_rate, audio=audio)
        if not audio:
            self._decoder = FmBitSource(self._rate)  # type: ignore[assignment]

    def _decode_buffer(self) -> list[ForesailRxFrame]:
        if not self._decoder.signal_present(self._buf):
            return []
        t0 = self._buf_start / self._rate
        fresh: list[ForesailRxFrame] = []
        for phase, raw in self._decoder.raw_bit_streams(self._buf):
            for position, inverted in _fs._candidate_positions(raw):
                bits: NDArray[np.uint8] = (raw ^ 1).astype(np.uint8) if inverted else raw
                decoded = decode_frame_at(bits, position)
                if decoded is None:
                    continue
                payload, corrected = decoded
                start = t0 + (phase / 8.0 + position) / BAUD
                end = start + (_SYNC_LEN + _CODEWORD_BYTES * 8) / BAUD
                previous = self._recent.get(payload)
                if previous is not None and abs(start - previous) < _DEDUP_S:
                    continue
                self._recent[payload] = start
                fresh.append(ForesailRxFrame(payload, start, end, 0, corrected))
        horizon = t0 - 2 * _fs._WINDOW_S
        for key in [k for k, v in self._recent.items() if v < horizon]:
            del self._recent[key]
        fresh.sort(key=lambda f: f.start_s)
        return fresh


def decode_iq(iq: NDArray[Any], sample_rate: float, *, audio: bool = False) -> list[ErminazRxFrame]:
    """Decode a whole recording (``audio=True``: 48 kHz discriminator audio) in one go."""
    stream = ErminazRxStream(sample_rate, audio=audio)
    frames = stream.feed(iq)
    frames.extend(stream.flush())
    return frames


class ErminazSdrDemod(ForesailSdrDemod):
    """Worker thread running an ``ErminazRxStream`` on SDR I/Q (or sound card audio)."""

    def __init__(self, sample_rate: int, parent: Any = None, *, audio: bool = False) -> None:
        super().__init__(sample_rate, parent, audio=audio)
        self._stream = ErminazRxStream(sample_rate, audio=audio)


class ErminazAudioDemod(ErminazSdrDemod):
    """The same worker for the rig's receive audio (48 kHz mono float)."""

    def __init__(self, sample_rate: int = AUDIO_RATE, parent: Any = None) -> None:
        super().__init__(sample_rate, parent, audio=True)
