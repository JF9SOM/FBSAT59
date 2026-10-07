"""Bell 202 AFSK transmit waveform, checked by an independent receiver and by Direwolf."""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import threading
import time

import numpy as np
import pytest
from numpy.typing import NDArray

from comms.aprs.afsk_tx import MARK_HZ, SPACE_HZ, build_afsk_audio
from comms.aprs.direwolf import _kiss_decode_frames, find_direwolf
from comms.aprs.g3ruh_tx import fcs16

_RATE = 48_000
_BAUD = 1200


def decode_afsk(audio: NDArray[np.float32]) -> list[bytes]:
    """Independent receiver: per bit, compare mark/space tone energy; NRZI; HDLC; FCS."""
    spb = _RATE // _BAUD
    n = len(audio) // spb
    t = np.arange(spb) / _RATE
    refs = [np.exp(-2j * np.pi * f * t) for f in (MARK_HZ, SPACE_HZ)]
    line = []
    for i in range(n):
        seg = audio[i * spb : (i + 1) * spb]
        mark = abs(np.dot(seg, refs[0]))
        space = abs(np.dot(seg, refs[1]))
        line.append(1 if mark > space else 0)
    bits = []
    prev = 0
    for b in line:  # NRZI: 1 = no change
        bits.append(1 if b == prev else 0)
        prev = b
    text = "".join(map(str, bits))
    frames: list[bytes] = []
    for seg_s in text.split("01111110"):
        if len(seg_s) < 8 * 19:
            continue
        seg_s = seg_s.replace("111110", "11111")
        k = len(seg_s) // 8
        raw = bytes(int(seg_s[i * 8 : i * 8 + 8][::-1], 2) for i in range(k))
        if fcs16(raw[:-2]).to_bytes(2, "little") == raw[-2:]:
            frames.append(raw[:-2])
    return frames


def _address(call: str, last: bool = False) -> bytes:
    return bytes((ord(c) << 1) for c in call.ljust(6)) + bytes([0x60 | (1 if last else 0)])


_FRAME = _address("APRS") + _address("JF9SOM", True) + b"\x03\xf0" + b":JA1XYZ   :hello{1"


@pytest.mark.parametrize(
    "frame",
    [_FRAME, bytes([0xFF] * 14) + b"\x03\xf0" + b"\xff\xff\x7e\x7e stuffing", bytes(range(20, 90))],
)
def test_round_trip_through_an_independent_receiver(frame: bytes) -> None:
    assert frame in decode_afsk(build_afsk_audio(frame))


def test_audio_is_a_continuous_full_scale_two_tone_signal() -> None:
    audio = build_afsk_audio(_FRAME)
    assert float(np.max(np.abs(audio))) == pytest.approx(1.0, abs=1e-3)
    assert len(audio) % (_RATE // _BAUD) == 0
    # continuous phase: no sample-to-sample jump beyond what a 2200 Hz sine allows
    assert float(np.max(np.abs(np.diff(audio)))) <= 2 * np.pi * SPACE_HZ / _RATE + 1e-3


def test_txdelay_lengthens_the_preamble() -> None:
    a = build_afsk_audio(_FRAME, txdelay_ms=300)
    b = build_afsk_audio(_FRAME, txdelay_ms=500)
    assert (len(b) - len(a)) / _RATE == pytest.approx(0.2, abs=0.01)


def test_rejects_a_bad_rate() -> None:
    with pytest.raises(ValueError):
        build_afsk_audio(b"x", baud=0)


@pytest.mark.skipif(find_direwolf() is None, reason="direwolf binary not installed")
def test_direwolf_decodes_our_afsk_audio() -> None:
    """Direwolf's own 1200 baud receiver reads the frame back."""
    audio = build_afsk_audio(_FRAME)
    rng = np.random.default_rng(5)
    lead = (rng.standard_normal(_RATE) * 0.005).astype(np.float32)
    pcm = (np.concatenate([lead, audio * 0.5, lead]) * 32767).astype("<i2").tobytes()
    port = 8900 + os.getpid() % 90
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "d.conf"), "w") as fh:
            fh.write(
                "MYCALL N0CALL\nADEVICE stdin null\nARATE 48000\nACHANNELS 1\nCHANNEL 0\n"
                f"MODEM 1200\nKISSPORT {port}\n"
            )
        binary = find_direwolf()
        assert binary is not None
        proc = subprocess.Popen(
            [str(binary), "-c", "d.conf", "-t", "0"],
            cwd=tmp,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        got: list[bytes] = []

        def read_kiss() -> None:
            deadline = time.time() + 20
            sock = None
            while time.time() < deadline and sock is None:
                try:
                    sock = socket.create_connection(("127.0.0.1", port), timeout=1)
                except OSError:
                    time.sleep(0.2)
            if sock is None:
                return
            sock.settimeout(1.0)
            buf = bytearray()
            while time.time() < deadline and not got:
                try:
                    chunk = sock.recv(512)
                except OSError:
                    continue
                if not chunk:
                    break
                buf.extend(chunk)
                got.extend(_kiss_decode_frames(buf))
            sock.close()

        reader = threading.Thread(target=read_kiss, daemon=True)
        reader.start()
        try:
            time.sleep(1.5)
            assert proc.stdin is not None
            for i in range(0, len(pcm), 4096):
                proc.stdin.write(pcm[i : i + 4096])
                proc.stdin.flush()
                time.sleep(4096 / 2 / _RATE)
            reader.join(timeout=15)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
    assert _FRAME in got
