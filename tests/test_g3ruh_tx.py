"""G3RUH transmit waveform: bit chain checked by an independent decoder and by Direwolf."""

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

from comms.aprs.direwolf import _kiss_decode_frames, find_direwolf
from comms.aprs.g3ruh_tx import build_g3ruh_audio, fcs16, scramble

_ARICA_UPLOAD = bytes([0x42, 0xF8, 0xBD, 0x70, 0x00]) + b"JF9SOM" + b"CQ PM86\x00"


def test_fcs_matches_the_official_arica2_example() -> None:
    # The lab page prints (9D 44) as the FCS of this uplink.
    frame = bytes.fromhex("42F8BD40004A5331595345414141000000000000"[:38])
    assert fcs16(frame).to_bytes(2, "little") == bytes([0x9D, 0x44])


def _decode(audio: NDArray[np.float32], baud: int = 4800, rate: int = 48_000) -> list[bytes]:
    """Independent receiver: sample each bit's end, descramble, NRZI, unstuff, check FCS."""
    spb = rate // baud
    x = [1 if audio[i * spb + spb - 1] < 0 else 0 for i in range(len(audio) // spb)]
    # G3RUH descrambler: d[n] = x[n] ^ x[n-17] ^ x[n-12]
    data = [
        x[n] ^ (x[n - 17] if n >= 17 else 0) ^ (x[n - 12] if n >= 12 else 0) for n in range(len(x))
    ]
    # NRZI decode: 1 = no change
    prev = 0
    bits = []
    for line in data:
        bits.append(1 if line == prev else 0)
        prev = line
    text = "".join(map(str, bits))
    frames: list[bytes] = []
    for seg in text.split("01111110"):
        if len(seg) < 16 + 8:
            continue
        seg = seg.replace("111110", "11111")  # drop the stuffed 0 after five 1s
        n = len(seg) // 8
        raw = bytes(int(seg[i * 8 : i * 8 + 8][::-1], 2) for i in range(n))
        if fcs16(raw[:-2]).to_bytes(2, "little") == raw[-2:]:
            frames.append(raw[:-2])
    return frames


@pytest.mark.parametrize(
    "frame",
    [
        _ARICA_UPLOAD,
        bytes([0xFF] * 12) + b"\x03\xf0" + b"stuffing heavy \xff\xff\xff\x7e\x7e",
        bytes(range(1, 60)),
    ],
)
@pytest.mark.parametrize(("baud", "rate"), [(4800, 48_000), (9600, 48_000)])
def test_round_trip_through_an_independent_decoder(frame: bytes, baud: int, rate: int) -> None:
    audio = build_g3ruh_audio(frame, baud=baud, sample_rate=rate)
    assert frame in _decode(audio, baud, rate)


def test_waveform_is_smooth_and_within_full_scale() -> None:
    audio = build_g3ruh_audio(_ARICA_UPLOAD)
    assert float(np.max(np.abs(audio))) <= 1.0 + 1e-6
    # half-cosine transitions: no sample-to-sample jump larger than sin(18 deg)
    assert float(np.max(np.abs(np.diff(audio)))) <= 0.31 + 1e-6
    # every bit is 10 samples at 4800 baud / 48 kHz
    assert len(audio) % 10 == 0


def test_preamble_and_tail_lengths_follow_txdelay_and_txtail() -> None:
    base = build_g3ruh_audio(_ARICA_UPLOAD, txdelay_ms=300, txtail_ms=100)
    longer = build_g3ruh_audio(_ARICA_UPLOAD, txdelay_ms=500, txtail_ms=100)
    extra_s = (len(longer) - len(base)) / 48_000
    assert extra_s == pytest.approx(0.2, abs=0.01)


def test_scrambler_matches_a_hand_computed_start() -> None:
    # With the register at zero the first outputs equal the input until the taps fill.
    assert scramble([1, 0, 1, 1, 0]) == [1, 0, 1, 1, 0]


def test_rejects_a_bad_rate() -> None:
    with pytest.raises(ValueError):
        build_g3ruh_audio(b"x", baud=0)


@pytest.mark.skipif(find_direwolf() is None, reason="direwolf binary not installed")
def test_direwolf_decodes_our_audio() -> None:
    """The decisive check: Direwolf's own 4800 G3RUH receiver reads the frame back."""
    audio = build_g3ruh_audio(_ARICA_UPLOAD)
    rng = np.random.default_rng(3)
    lead = (rng.standard_normal(48_000) * 0.01).astype(np.float32)
    pcm = (np.concatenate([lead, audio * 0.5, lead]) * 32767).astype("<i2").tobytes()

    port = 8700 + os.getpid() % 200
    with tempfile.TemporaryDirectory() as tmp:
        conf = os.path.join(tmp, "d.conf")
        with open(conf, "w") as fh:
            fh.write(
                "MYCALL N0CALL\nADEVICE stdin null\nARATE 48000\nACHANNELS 1\nCHANNEL 0\n"
                f"MODEM 4800 G3RUH\nKISSPORT {port}\n"
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
            step = 4096
            for i in range(0, len(pcm), step):
                proc.stdin.write(pcm[i : i + step])
                proc.stdin.flush()
                time.sleep(step / 2 / 48_000)
            reader.join(timeout=15)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
    assert _ARICA_UPLOAD in got
