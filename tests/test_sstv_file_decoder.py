"""
Unit tests for comms/sstv/file_decoder.py — MP3/WAV -> mono PCM loading used
by the SSTV tab's "Decode Recording…" button.

Skipped entirely when soundfile is not installed (matches how the SSTV tab
itself degrades: the button is disabled with a tooltip — see sstv_tab.py).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from comms.sstv.file_decoder import SOUNDFILE_AVAILABLE, load_audio_mono

pytestmark = pytest.mark.skipif(not SOUNDFILE_AVAILABLE, reason="soundfile not installed")


def _write_wav_tone(
    path: str, freq: float, seconds: float, sample_rate: int
) -> NDArray[np.float32]:
    import soundfile as sf

    t = np.arange(int(sample_rate * seconds)) / sample_rate
    tone = (0.3 * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)
    sf.write(path, tone, sample_rate)
    return tone


def test_load_audio_mono_same_rate_roundtrips(tmp_path: Path) -> None:
    """No resampling needed when the file's rate already matches target_rate."""
    path = str(tmp_path / "tone.wav")
    tone = _write_wav_tone(path, freq=1000.0, seconds=1.0, sample_rate=44100)

    loaded = load_audio_mono(path, target_rate=44100)

    assert loaded.dtype == np.float32
    assert len(loaded) == len(tone)
    # Same signal, same rate -> should match closely (WAV is lossless).
    assert np.allclose(loaded, tone, atol=1e-3)


def test_load_audio_mono_resamples_to_target_rate(tmp_path: Path) -> None:
    """A 48kHz file (our own AudioRecorder's rate) resampled down to 44100."""
    path = str(tmp_path / "tone_48k.wav")
    _write_wav_tone(path, freq=1500.0, seconds=2.0, sample_rate=48000)

    loaded = load_audio_mono(path, target_rate=44100)

    # 2.0s of audio at 48000Hz -> ~2.0s worth of samples at 44100Hz.
    expected_len = int(44100 * 2.0)
    assert abs(len(loaded) - expected_len) < 100
    assert loaded.dtype == np.float32


def test_load_audio_mono_downmixes_stereo(tmp_path: Path) -> None:
    """Stereo input is averaged down to mono before resampling."""
    import soundfile as sf

    sample_rate = 44100
    t = np.arange(sample_rate) / sample_rate
    left = (0.3 * np.sin(2.0 * np.pi * 1000.0 * t)).astype(np.float32)
    right = (0.3 * np.sin(2.0 * np.pi * 1000.0 * t)).astype(np.float32)
    stereo = np.stack([left, right], axis=1)
    path = str(tmp_path / "stereo.wav")
    sf.write(path, stereo, sample_rate)

    loaded = load_audio_mono(path, target_rate=44100)

    assert loaded.ndim == 1
    assert len(loaded) == sample_rate
    assert np.allclose(loaded, left, atol=1e-3)


def test_load_audio_mono_raises_without_soundfile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Guard path used when soundfile is not installed at all."""
    import comms.sstv.file_decoder as file_decoder_module

    monkeypatch.setattr(file_decoder_module, "SOUNDFILE_AVAILABLE", False)
    monkeypatch.setattr(file_decoder_module, "_soundfile", None)

    with pytest.raises(RuntimeError, match="soundfile is not installed"):
        file_decoder_module.load_audio_mono(str(tmp_path / "missing.wav"), target_rate=44100)
