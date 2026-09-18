"""File-backed pseudo SdrDevice for replaying a recorded IQ WAV file.

Lets SDRPipeline -- and everything built on top of it: the waterfall,
and Telemetry/FT4/Q65/SSTV's Direwolf-fed decoders, all of which reach it
via whichever Rig slot's SdrRigAdapter currently has a pipeline attached
-- run completely unmodified against a saved .iq.wav recording instead of
live hardware. SDRPipeline only ever calls five members on self._device
(see its own module docstring / read_samples() call sites):
sample_rate, center_freq, start_stream(), read_samples(), stop_stream().
This class duck-types all five, plus set_center_freq() and close() for
SdrRigAdapter's disconnect()/set_frequency() paths.

center_freq is fixed at 0.0 for the lifetime of the device. SDRPipeline's
_apply_doppler_correction() only resets its NCO phase when center_freq
*changes* (a real hardware retune breaking phase continuity), so keeping
it constant means the manual "Offset" control in SdrControlWidget's
playback panel -- which drives SDRPipeline.set_doppler_target() directly
-- behaves exactly like live SDR's Passband Tune: a smooth,
phase-continuous shift with no click between adjustments, and a signal
that really is at the recording's own baseband reference always displays
at its true recorded position regardless of the current Offset guess (see
docs/sdr.md's playback section for the full frame-of-reference
reasoning -- the short version: the waterfall's "target" marker moves
with Offset, the actual signal doesn't, and they coincide exactly when
Offset matches the true drift).

Reads the whole WAV into memory up front -- a several-minute VHF/UHF pass
at a typical recording sample rate (e.g. 250 kHz) is a few tens of MB at
most -- so seeking (SdrControlWidget's playback position slider) is just
an index write under a lock, no streaming file I/O or OS-level seek call,
trivially thread-safe against the pipeline thread's concurrent reads.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


class SdrFileDevice:
    """Duck-typed SdrDevice that replays a recorded .iq.wav file."""

    def __init__(self, wav_path: Path) -> None:
        # Imported here, not at module level: scipy is an optional
        # dependency (see CLAUDE.md's optional-import notes) and every
        # other sdr.* module already avoids a hard import-time
        # dependency on it for the same reason (recorder.py).
        import scipy.io.wavfile as wav

        rate, data = wav.read(str(wav_path))
        # recorder.py always writes float32 stereo, I=left (real), Q=right
        # (imag) -- see comms.aprs... no, sdr.recorder's module docstring.
        data = np.asarray(data, dtype=np.float32)
        self._samples: np.ndarray = (data[:, 0] + 1j * data[:, 1]).astype(np.complex64)
        self._sample_rate: float = float(rate)
        self._lock = threading.Lock()
        self._pos: int = 0
        self._streaming = False
        logger.info(
            "SdrFileDevice: loaded %s (%.1fs at %.0f Hz)",
            wav_path,
            self.duration_s,
            self._sample_rate,
        )

    # ------------------------------------------------------------------
    # SDRPipeline's required interface
    # ------------------------------------------------------------------

    @property
    def sample_rate(self) -> float:
        return self._sample_rate

    @property
    def center_freq(self) -> float:
        """Fixed at 0.0 -- see module docstring for why."""
        return 0.0

    def set_center_freq(self, freq_hz: float) -> bool:
        """No-op: center_freq never moves for a file-backed device."""
        return True

    def start_stream(self) -> bool:
        self._streaming = True
        return True

    def stop_stream(self) -> None:
        self._streaming = False

    def read_samples(self, num_samples: int = 1024) -> np.ndarray | None:
        """Return the next *num_samples* samples, paced to real time.

        Returns None once playback has reached the end of the file (the
        same "nothing available right now" convention SdrDevice.
        read_samples() uses for a timeout), so SDRPipeline.run() just
        idles rather than erroring. SdrControlWidget's playback timer is
        what actually notices end-of-file (via at_end) and stops
        playback -- this method only ever pauses, never restarts from 0.
        """
        if not self._streaming:
            return None
        with self._lock:
            start = self._pos
            end = min(start + num_samples, len(self._samples))
            if start >= end:
                return None
            block = self._samples[start:end].copy()
            self._pos = end
        # Real hardware paces reads by blocking on the USB transfer; a
        # from-memory read is instant, so sleep out the equivalent
        # wall-clock duration instead -- otherwise Direwolf's bit-sync
        # timing (and the waterfall/UI update rate) would see samples
        # arrive far faster than the baud rate they were recorded at.
        time.sleep(len(block) / self._sample_rate)
        return block

    def close(self) -> None:
        self._streaming = False
        with self._lock:
            self._samples = np.zeros(0, dtype=np.complex64)

    # ------------------------------------------------------------------
    # Playback-specific extensions (SdrRigAdapter.seek_file() etc.)
    # ------------------------------------------------------------------

    @property
    def duration_s(self) -> float:
        return len(self._samples) / self._sample_rate if self._sample_rate else 0.0

    @property
    def position_s(self) -> float:
        with self._lock:
            return self._pos / self._sample_rate if self._sample_rate else 0.0

    @property
    def at_end(self) -> bool:
        with self._lock:
            return self._pos >= len(self._samples)

    def seek(self, position_s: float) -> None:
        """Jump the read position to *position_s* seconds from the start."""
        with self._lock:
            idx = int(position_s * self._sample_rate)
            self._pos = max(0, min(idx, len(self._samples)))
