"""
SdrPlugin abstract base class.

Every SDR plugin (built-in or future) must subclass SdrPlugin.  The plugin
system allows new data-mode decoders to be added without modifying the
pipeline or SDR Control widget.

Built-in plugins (initial release):
  FmDemodPlugin     — NFM audio demodulation
  SsbCwDemodPlugin  — USB/LSB/CW audio demodulation
  IqRecorderPlugin  — Raw I/Q WAV recording

Future plugins (phase 2+):
  SatDumpPlugin     — HRPT/LRPT satellite imagery via SatDump subprocess
  DirewolfPlugin    — APRS via Direwolf TCP KISS
  WsjtxPlugin       — FT4 via WSJT-X UDP
  SstvPlugin        — SSTV reception (comms.sstv.decoder: Robot36 / PD120)

Audio input source (for data mode plugins that accept voice audio):
  SdrAudioSource    — software-demodulated audio from the I/Q pipeline
  SoundcardSource   — system audio input device (rig AF output)
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from enum import Enum, auto

import numpy as np
from PySide6.QtWidgets import QWidget


class AudioSourceType(Enum):
    """Audio input source for data-mode plugins."""

    SDR = auto()  # I/Q → software demodulation
    SOUNDCARD = auto()  # System audio device (e.g. rig AF output)


class SdrPlugin(ABC):
    """
    Abstract base class for all SDR plugins.

    Subclasses must implement:
      name, supported_modes, get_widget(), start(), stop(), is_available()

    Optional:
      requires_tx_audio   — set True if TX via rig soundcard is needed
      requires_external   — name of an external binary (e.g. "direwolf")
      on_iq_samples()     — called by pipeline with each I/Q block
      on_audio_samples()  — called with each demodulated PCM block
    """

    # -- Class attributes (override in subclasses) --

    #: Display name shown in the SDR Control tab
    name: str = "Unnamed Plugin"

    #: SATNOGS mode strings this plugin handles (empty = all modes)
    supported_modes: list[str] = []

    #: True if the plugin needs to transmit via a rig soundcard
    requires_tx_audio: bool = False

    #: Name of required external binary (None = no external dependency)
    requires_external: str | None = None

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    def get_widget(self) -> QWidget:
        """Return the QWidget to embed in the SDR Control tab."""

    @abstractmethod
    def start(self, center_freq_hz: float, sample_rate_hz: float) -> None:
        """Begin processing.  Called when the user activates this plugin."""

    @abstractmethod
    def stop(self) -> None:
        """Stop processing and release resources."""

    def is_available(self) -> bool:
        """
        Return True if this plugin can run in the current environment.

        Default implementation checks whether requires_external is in PATH.
        Override for more complex checks.
        """
        if self.requires_external is None:
            return True
        return shutil.which(self.requires_external) is not None

    # ------------------------------------------------------------------
    # Optional hooks (called from SDRPipeline thread)
    # ------------------------------------------------------------------

    def on_iq_samples(self, iq: np.ndarray) -> None:  # noqa: B027
        """Receive a block of complex64 I/Q samples from the pipeline."""

    def on_audio_samples(self, pcm: np.ndarray) -> None:  # noqa: B027
        """Receive a block of float32 PCM audio (post-demodulation)."""

    # ------------------------------------------------------------------
    # Audio source selection (for data-mode plugins)
    # ------------------------------------------------------------------

    def set_audio_source(
        self, source_type: AudioSourceType, device_index: int | None = None
    ) -> None:
        """
        Select the audio input source.

        source_type: SDR (software demod) or SOUNDCARD (rig AF input)
        device_index: sounddevice device index (ignored for SDR source)
        """
        self._audio_source = source_type
        self._audio_device_index = device_index

    @property
    def audio_source(self) -> AudioSourceType:
        return getattr(self, "_audio_source", AudioSourceType.SDR)
