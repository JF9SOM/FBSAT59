"""Tests for comms.aprs.tx_audio_log (receive audio saved around each APRS transmission)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from comms.aprs import tx_audio_log
from comms.aprs.tx_audio_log import SAVED_RATE, TxAudioRecorder


def _feed_seconds(rec: TxAudioRecorder, start: float, seconds: float) -> None:
    """Feed *seconds* of 48 kHz audio in 0.1 s chunks; chunk times end at start+0.1, ..."""
    for i in range(int(seconds * 10)):
        rec.feed(np.full(4800, 0.25, dtype=np.float32), now=start + 0.1 * (i + 1))


def test_saves_window_around_transmission(tmp_path: Path) -> None:
    rec = TxAudioRecorder(out_dir=tmp_path)
    _feed_seconds(rec, 100.0, 40.0)  # audio up to t=140
    path = rec.save(tx_on=120.0, tx_off=121.5)
    assert path is not None and path.exists()
    import soundfile as sf

    data, rate = sf.read(str(path))
    assert rate == SAVED_RATE
    # 5 s pre-roll + 1.5 s transmission + up to 12 s post-roll (only 18.5 s were recorded after).
    assert 18.0 < len(data) / rate < 19.5
    assert "on5.0" in path.name and "off6.5" in path.name


def test_no_audio_buffered_returns_none(tmp_path: Path) -> None:
    rec = TxAudioRecorder(out_dir=tmp_path)
    assert rec.save(tx_on=10.0, tx_off=11.0) is None
    assert list(tmp_path.iterdir()) == []


def test_ring_drops_audio_older_than_the_ring_length(tmp_path: Path) -> None:
    rec = TxAudioRecorder(out_dir=tmp_path)
    _feed_seconds(rec, 0.0, 120.0)
    assert rec._ring[0][0] > 120.0 - 61.0


def test_keeps_only_the_newest_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(tx_audio_log, "_KEEP_FILES", 3)
    rec = TxAudioRecorder(out_dir=tmp_path)
    _feed_seconds(rec, 0.0, 30.0)
    for i in range(6):
        rec.save(tx_on=10.0 + i, tx_off=11.0 + i)
    assert len(list(tmp_path.iterdir())) <= 3


def test_decimated_chunk_is_a_quarter_the_length() -> None:
    out = tx_audio_log._decimate(np.arange(4800, dtype=np.float32))
    assert len(out) == 1200


def test_note_tx_end_without_start_does_nothing(tmp_path: Path) -> None:
    rec = TxAudioRecorder(out_dir=tmp_path)
    rec.note_tx_end(delay_s=0.0)
    assert rec._pending == []
