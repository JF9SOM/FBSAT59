"""The audio handed to the sound card is saved so it can be decoded offline."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from comms.aprs.tx_audio_log import SENT_PREFIX, save_sent_audio


def test_sent_audio_is_saved_losslessly_at_the_playback_rate(tmp_path: Path) -> None:
    audio = (0.005 * np.sin(2 * np.pi * 1200 * np.arange(4800) / 48000)).astype(np.float32)
    path = save_sent_audio("APRS", audio, 48000, out_dir=tmp_path)
    assert path is not None and path.name.startswith(f"{SENT_PREFIX}APRS_")
    data, rate = sf.read(str(path))
    assert rate == 48000
    assert np.max(np.abs(data - audio)) < 1e-6  # 24-bit keeps even a -40 dB signal intact


def test_only_the_newest_sent_files_are_kept(tmp_path: Path) -> None:
    for i in range(65):
        (tmp_path / f"{SENT_PREFIX}APRS_{i:03d}.flac").write_bytes(b"x")
    save_sent_audio("APRS", np.zeros(48, dtype=np.float32), 48000, out_dir=tmp_path)
    assert len(list(tmp_path.glob(f"{SENT_PREFIX}*"))) == 60


def test_failure_never_raises() -> None:
    assert save_sent_audio("APRS", None, 48000, out_dir=Path("/nonexistent/x/y")) is None
