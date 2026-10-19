"""Tests for the gr-satellites fast decode of an IQ recording file.

No gr_satellites needed: the subprocess is a small Python script printing canned output.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

import comms.telemetry.gr_satellites_file as gr_file
from comms.telemetry.iq_wav import iter_iq_blocks, read_wav_info

_FAKE_GR = """
import sys, time
for i in range(3):
    print("***** VERBOSE PDU DEBUG PRINT ******")
    print("pdu length = 2 bytes")
    print("0000: 12 %02x " % i)
    print("************************************")
    sys.stdout.flush()
    time.sleep(0.05)
"""


def _recording(tmp_path: Path, seconds: float = 2.0, rate: int = 48000) -> Path:
    samples = (np.arange(int(seconds * rate)) % 7).astype(np.complex64) * (1 + 1j)
    path = tmp_path / "rec.iq.wav"
    gr_file.write_iq_wav(path, samples, rate)
    return path


def test_write_iq_wav_round_trips(tmp_path: Path) -> None:
    path = _recording(tmp_path, 1.0, 8000)
    info = read_wav_info(path)
    assert (info.sample_rate, info.frames, info.float_samples) == (8000.0, 8000, True)
    block = next(iter_iq_blocks(path, info))
    assert block[3] == pytest.approx(3 + 3j)


def test_estimate_frame_seconds_maps_arrival_to_position() -> None:
    # 2 s of overhead in a 22 s run: reading takes 20 s, starting after 1 s
    assert gr_file.estimate_frame_seconds(11.0, 22.0, 2.0, 600.0) == pytest.approx(300.0)
    assert gr_file.estimate_frame_seconds(0.0, 22.0, 2.0, 600.0) == 0.0
    assert gr_file.estimate_frame_seconds(30.0, 22.0, 2.0, 600.0) == 600.0


def _decode(path: Path, tmp_path: Path) -> tuple[list[tuple[str, float]], list[str], list[tuple]]:
    script = tmp_path / "fake_gr.py"
    script.write_text(_FAKE_GR)
    frames: list[tuple[str, float]] = []
    failures: list[str] = []
    done: list[tuple] = []
    with (
        patch.object(
            gr_file,
            "resolve_gr_satellites_command",
            return_value=([sys.executable, str(script)], True),
        ),
        patch.object(gr_file, "satellite_argument", return_value="67284") as arg,
    ):
        job = gr_file.GrFileDecoder(path, 67284)
        job.frame_found.connect(lambda t, s: frames.append((t, s)))
        job.failed.connect(failures.append)
        job.decoded.connect(lambda d, e, n: done.append((d, e, n)))
        job.run()
        assert arg.called
    return frames, failures, done


def test_decodes_a_recording_and_times_the_frames_inside_it(tmp_path: Path) -> None:
    path = _recording(tmp_path)
    frames, failures, done = _decode(path, tmp_path)
    assert failures == []
    assert len(frames) == 3
    assert "0000: 12 01" in frames[1][0]
    seconds = [s for _, s in frames]
    assert seconds == sorted(seconds)
    assert all(0.0 <= s <= 2.0 for s in seconds)
    assert done[0][0] == pytest.approx(2.0)
    assert done[0][2] == 3


def test_a_file_that_is_not_an_iq_wav_fails(tmp_path: Path) -> None:
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav file at all")
    frames, failures, done = _decode(bad, tmp_path)
    assert frames == [] and done == []
    assert len(failures) == 1
