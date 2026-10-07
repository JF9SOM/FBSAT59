"""Play one transmit audio buffer through the Sound Card and key the rig's PTT.

Shared by the ARICA-2 message box and the APRS tab. It is a plain thread body,
not a QThread: sounddevice's play/wait blocks and needs no Qt event loop (same
pattern as the FT4 and AX100 transmit workers). Exactly one of ``finished`` or
``error`` is emitted per ``run()``.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QObject, Signal

from comms.audio_device_manager import get_audio_device_manager
from i18n import _

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
        if not mgr.acquire_output(self._owner, self._out_device):
            other = mgr.output_owner(self._out_device) or _("another tab")
            self.error.emit(_("Sound card output is in use by {other}").format(other=other))
            return
        try:
            import sounddevice as sd  # optional dependency

            if not self._rig.set_ptt(True):
                self.error.emit(_("PTT command failed — check Rig 1 connection"))
                return
            time.sleep(PTT_LEAD_S)
            sd.play(self._audio, samplerate=AUDIO_RATE, device=self._out_device, blocking=False)
            mgr.pin_active_output(self._owner)
            sd.wait()
            time.sleep(PTT_TAIL_S)
            self._rig.set_ptt(False)
            self.finished.emit()
        except Exception as exc:
            with contextlib.suppress(Exception):
                self._rig.set_ptt(False)
            self.error.emit(str(exc))
        finally:
            mgr.release_output(self._owner, self._out_device)
