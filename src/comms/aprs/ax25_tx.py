"""One entry point that turns an AX.25 frame into transmit audio for a modem speed."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from comms.aprs.afsk_tx import build_afsk_audio
from comms.aprs.g3ruh_tx import build_g3ruh_audio

SAMPLE_RATE = 48_000


def build_ax25_audio(frame: bytes, modem: str) -> NDArray[np.float32]:
    """Audio for *frame* (no FCS) at *modem* "1200" (Bell 202 AFSK), "4800" or "9600" (G3RUH)."""
    if modem == "1200":
        return build_afsk_audio(frame, baud=1200, sample_rate=SAMPLE_RATE)
    if modem in ("4800", "9600"):
        return build_g3ruh_audio(frame, baud=int(modem), sample_rate=SAMPLE_RATE)
    raise ValueError(f"unsupported modem {modem!r}")
