"""1200 baud AFSK file decoder (Telemetry tab "Fast").

The plumbing with a stand-in Direwolf, and (if Direwolf is installed) the real one.
"""

from __future__ import annotations

import math
import struct
import sys
import textwrap
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

import comms.aprs.afsk_file_decoder as afd
from comms.aprs.afsk_file_decoder import Afsk1200FileDecoder, _free_port, _to_pcm
from comms.aprs.direwolf import _kiss_encode, find_direwolf

RATE = 48_000


def _write_iq(path: Path, iq: np.ndarray, rate: int) -> None:
    pairs = np.stack([iq.real, iq.imag], axis=1).astype("<f4").tobytes()
    fmt = struct.pack("<HHIIHH", 3, 2, rate, rate * 8, 8, 32)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"data" + struct.pack("<I", len(pairs))
    path.write_bytes(
        b"RIFF" + struct.pack("<I", 4 + len(chunks) + len(pairs)) + b"WAVE" + chunks + pairs
    )


class _StubDiscriminator:
    """Stand-in for the SDR audio front end (it needs scipy): the I/Q's real part is the audio."""

    def __init__(self, input_rate: float, satellite: bool = False) -> None:
        self.satellite = satellite

    def process(self, iq: np.ndarray) -> np.ndarray:
        return iq.real.astype(np.float32)


# A stand-in for Direwolf: opens the KISS port named in the config, counts the audio on stdin,
# reports one frame once a second of audio has arrived and one more at the end of the audio.
_FAKE_DIREWOLF = textwrap.dedent(
    """
    import re, socket, sys, os
    conf = open(sys.argv[1]).read()
    port = int(re.search(r"KISSPORT (\\d+)", conf).group(1))
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)
    client, _ = server.accept()
    def kiss(payload):
        return b"\\xc0\\x00" + payload + b"\\xc0"
    seen = 0
    sent_first = False
    while True:
        data = os.read(0, 65536)
        if not data:
            break
        seen += len(data)
        if not sent_first and seen >= 96000:  # one second of 48 kHz 16 bit audio
            client.sendall(kiss(b"FIRST"))
            sent_first = True
    client.sendall(kiss(b"LAST"))
    import time
    time.sleep(0.3)
    """
)


def _run(qtbot: QtBot, job: Afsk1200FileDecoder) -> tuple[list[tuple[bytes, float]], list[str]]:
    frames: list[tuple[bytes, float]] = []
    failures: list[str] = []
    job.frame_found.connect(lambda raw, t: frames.append((raw, t)))
    job.failed.connect(failures.append)
    with qtbot.waitSignals([job.decoded], timeout=30000, raising=False):
        job.start()
    job.wait(5000)
    return frames, failures


def test_pcm_conversion_matches_the_live_path_and_clips() -> None:
    pcm = np.frombuffer(_to_pcm(np.array([0.0, 0.5, -0.5, 2.0, -2.0], dtype=np.float32)), "<i2")
    assert pcm.tolist() == [0, 16383, -16383, 32767, -32767]


def test_free_port_is_in_the_range_direwolf_accepts() -> None:
    for _ in range(20):
        assert 1024 <= _free_port() <= 49151


def test_frames_are_read_back_with_the_audio_position_as_their_time(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "fake_direwolf.py"
    fake.write_text(_FAKE_DIREWOLF)
    path = tmp_path / "rec.iq.wav"
    _write_iq(path, (np.ones(RATE * 4) * 0.1 + 0j).astype(np.complex64), RATE)  # 4 s
    monkeypatch.setattr(afd, "AfskAudioDiscriminator", _StubDiscriminator)
    monkeypatch.setattr(afd, "find_direwolf", lambda: Path(sys.executable))
    monkeypatch.setattr(
        Afsk1200FileDecoder, "_command", lambda self, binary, conf: [str(binary), str(fake), conf]
    )
    job = Afsk1200FileDecoder(path)
    done: list[tuple[float, float, int]] = []
    job.decoded.connect(lambda d, e, n: done.append((d, e, n)))
    progress: list[float] = []
    job.progress.connect(lambda fraction, n: progress.append(fraction))
    frames, failures = _run(qtbot, job)
    assert failures == []
    assert [raw for raw, _t in frames] == [b"FIRST", b"LAST"]
    # FIRST comes once a second of audio went in (the whole 4 s block is written at once, so it is
    # stamped with the position after that block); LAST after the 4 s plus the 2 s of tail.
    assert 1.0 <= frames[0][1] <= 4.0
    assert frames[1][1] == pytest.approx(6.0, abs=0.5)
    assert done and done[0][0] == pytest.approx(4.0) and done[0][2] == 2
    assert progress and progress[-1] == 1.0


def test_a_missing_direwolf_is_reported(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rec.iq.wav"
    _write_iq(path, np.zeros(RATE, dtype=np.complex64), RATE)
    monkeypatch.setattr(afd, "find_direwolf", lambda: None)
    job = Afsk1200FileDecoder(path)
    errors: list[str] = []
    job.failed.connect(errors.append)
    with qtbot.waitSignal(job.failed, timeout=10000):
        job.start()
    job.wait(5000)
    assert errors and "Direwolf" in errors[0]


def test_an_unreadable_file_is_reported(qtbot: QtBot, tmp_path: Path) -> None:
    path = tmp_path / "bad.iq.wav"
    path.write_bytes(b"not a wav file")
    job = Afsk1200FileDecoder(path)
    errors: list[str] = []
    job.failed.connect(errors.append)
    with qtbot.waitSignal(job.failed, timeout=10000):
        job.start()
    job.wait(5000)
    assert errors


def test_cancel_stops_the_job_without_a_result(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "fake_direwolf.py"
    fake.write_text(_FAKE_DIREWOLF)
    path = tmp_path / "rec.iq.wav"
    _write_iq(path, (np.ones(RATE * 30) * 0.1 + 0j).astype(np.complex64), RATE)
    monkeypatch.setattr(afd, "AfskAudioDiscriminator", _StubDiscriminator)
    monkeypatch.setattr(afd, "find_direwolf", lambda: Path(sys.executable))
    monkeypatch.setattr(
        Afsk1200FileDecoder, "_command", lambda self, binary, conf: [str(binary), str(fake), conf]
    )
    job = Afsk1200FileDecoder(path)
    results: list[Any] = []
    job.decoded.connect(lambda *args: results.append(args))
    job.start()
    qtbot.wait(300)
    job.cancel()
    assert not job.isRunning()
    assert results == []


# --- the real thing: a synthetic FM AFSK recording through the actual Direwolf ------------


def _ax25(dest: str, src: str, info: bytes) -> bytes:
    def address(call: str, last: bool) -> bytes:
        return bytes(ord(c) << 1 for c in call.ljust(6)) + bytes([0x60 | int(last)])

    return address(dest, False) + address(src, True) + b"\x03\xf0" + info


def _afsk_audio(frame: bytes) -> np.ndarray:
    from comms.aprs.g3ruh_tx import hdlc_nrzi_bits

    line = hdlc_nrzi_bits(frame, 30, 4)
    sps = RATE / 1200.0
    phase = 0.0
    parts = []
    for i, bit in enumerate(line):
        tone = 1200.0 if bit else 2200.0
        n = int(round((i + 1) * sps)) - int(round(i * sps))
        parts.append(np.sin(phase + 2 * math.pi * tone * np.arange(n) / RATE))
        phase += 2 * math.pi * tone * n / RATE
    return np.concatenate(parts)


@pytest.mark.skipif(find_direwolf() is None, reason="Direwolf is not installed")
def test_real_direwolf_decodes_a_synthetic_fm_afsk_recording(qtbot: QtBot, tmp_path: Path) -> None:
    pytest.importorskip("scipy")
    from scipy import signal

    frames = [_ax25("CQ", f"JA{i}ABC", f"fast decode test {i}".encode()) for i in (1, 2, 3)]
    audio = np.concatenate(
        [np.zeros(int(0.7 * RATE))]
        + [part for fr in frames for part in (_afsk_audio(fr), np.zeros(int(1.5 * RATE)))]
    )
    fs = 250_000
    divisor = math.gcd(fs, RATE)
    upsampled = signal.resample_poly(audio, fs // divisor, RATE // divisor)
    carrier = np.exp(1j * 2 * np.pi * np.cumsum(upsampled * 3000.0) / fs)
    rng = np.random.default_rng(3)
    noise_power = 10 ** (-20 / 10) * (fs / 2) / 12_000.0
    noise = (
        rng.standard_normal(len(carrier)) + 1j * rng.standard_normal(len(carrier))
    ) * math.sqrt(noise_power / 2)
    path = tmp_path / "afsk.iq.wav"
    _write_iq(path, (carrier + noise).astype(np.complex64), fs)
    found, failures = _run(qtbot, Afsk1200FileDecoder(path))
    assert failures == []
    assert [raw for raw, _t in found] == frames
    times = [t for _raw, t in found]
    assert times == sorted(times) and 1.0 < times[0] < 3.0
    assert _kiss_encode(frames[0])  # the helper the job decodes with is the one the app uses
