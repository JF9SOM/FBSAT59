"""Play one transmit audio buffer through the Sound Card and key the rig's PTT.

Shared by the ARICA-2 message box and the APRS tab. It is a plain thread body,
not a QThread: sounddevice's play/wait blocks and needs no Qt event loop (same
pattern as the FT4 and AX100 transmit workers). Exactly one of ``finished`` or
``error`` is emitted per ``run()``.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QObject, Signal

from comms.audio_device_manager import get_audio_device_manager
from i18n import _

logger = logging.getLogger(__name__)

AUDIO_RATE = 48_000
# The PTT goes up this long before the audio starts and stays up after it ends: rig
# key-up time and modulator settling (the audio itself begins with TXDELAY of flags).
PTT_LEAD_S = 0.20
PTT_TAIL_S = 0.20


class PttAudioTxWorker(QObject):
    """Keys the PTT, plays *audio* on *out_device*, releases the PTT."""

    finished: Signal = Signal()
    error: Signal = Signal(str)

    def __init__(
        self,
        owner: str,
        audio: NDArray[np.float32],
        out_device: int | None,
        rig: Any,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._owner = owner
        self._audio = audio
        self._out_device = out_device
        self._rig = rig

    def run(self) -> None:
        mgr = get_audio_device_manager()
        logger.info(
            "TX[%s]: start device=%s samples=%d (%.2f s) peak=%.3f",
            self._owner,
            self._out_device,
            len(self._audio),
            len(self._audio) / AUDIO_RATE,
            float(np.max(np.abs(self._audio))) if len(self._audio) else 0.0,
        )
        if not mgr.acquire_output(self._owner, self._out_device):
            other = mgr.output_owner(self._out_device) or _("another tab")
            logger.error(
                "TX[%s]: output device %s is held by %s", self._owner, self._out_device, other
            )
            self.error.emit(_("Sound card output is in use by {other}").format(other=other))
            return
        try:
            import sounddevice as sd  # optional dependency

            logger.info(
                "TX[%s]: PortAudio output device: %s", self._owner, _describe(sd, self._out_device)
            )
            keyed = self._rig.set_ptt(True)
            logger.info("TX[%s]: PTT on -> %s", self._owner, keyed)
            if not keyed:
                self.error.emit(_("PTT command failed — check Rig 1 connection"))
                return
            time.sleep(PTT_LEAD_S)
            sd.play(self._audio, samplerate=AUDIO_RATE, device=self._out_device, blocking=False)
            logger.info("TX[%s]: audio playback started", self._owner)
            mgr.pin_active_output(self._owner)
            sd.wait()
            time.sleep(PTT_TAIL_S)
            unkeyed = self._rig.set_ptt(False)
            logger.info("TX[%s]: playback finished, PTT off -> %s", self._owner, unkeyed)
            self.finished.emit()
        except Exception as exc:
            logger.exception("TX[%s]: failed", self._owner)
            with contextlib.suppress(Exception):
                self._rig.set_ptt(False)
            self.error.emit(str(exc))
        finally:
            mgr.release_output(self._owner, self._out_device)


def _describe(sd: Any, device: int | None) -> str:
    """Name and channel count PortAudio currently has for *device* (for the TX log)."""
    try:
        info = sd.query_devices(device, "output")
        channels = info["max_output_channels"]
        return f"{info['name']!r} out_ch={channels} rate={info['default_samplerate']}"
    except Exception as exc:
        return f"unavailable ({exc})"
