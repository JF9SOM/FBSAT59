"""APRS transmit frame and audio must equal what Direwolf 1.8.1 puts on the air."""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from comms.aprs.afsk_tx import build_afsk_audio
from comms.aprs.engine import (
    _build_aprs_message,
    _build_aprs_position,
    _build_uiss_message,
    describe_ax25_frame,
    parse_via_path,
)
from comms.aprs.g3ruh_tx import build_g3ruh_audio

# Frame of "JF9SOM>APRS,ARISS::JA1ABC   :hello world\n" as ``gen_packets`` / ``atest -h``
# report it: both the destination and the source SSID byte are 0xE0 (C bits set, "cc=11"),
# the digipeater SSID byte is 0x61 (H clear, last address).
_DIREWOLF_FRAME = bytes.fromhex(
    "82a0a4a64040e0948c72a69e9ae082a492a6a6406103f03a4a41314142432020203a68656c6c6f20776f726c640a"
)


def test_message_frame_is_identical_to_direwolfs() -> None:
    frame = _build_aprs_message("JF9SOM", 0, "ARISS", "JA1ABC", "hello world\n")
    assert frame == _DIREWOLF_FRAME


def test_uiss_message_puts_the_addressee_in_the_destination_field() -> None:
    frame = _build_uiss_message("JF9SOM", 0, "DP0SNX", "JK4DHT", "UR599")
    assert describe_ax25_frame(frame).startswith("JF9SOM>JK4DHT,DP0SNX info='UR599'")
    assert frame[6] == 0xE0 and frame[13] == 0xE0 and frame[20] == 0x61
    cq = _build_uiss_message("JF9SOM", 0, "DP0SNX", "cq", "PM86")
    assert describe_ax25_frame(cq).startswith("JF9SOM>CQ,DP0SNX info='PM86'")


def test_uiss_message_destination_may_carry_an_ssid() -> None:
    frame = _build_uiss_message("JF9SOM", 0, "DP0SNX", "JK4DHT-1", "hi")
    assert frame[6] == 0xE0 | (1 << 1)
    assert describe_ax25_frame(frame).startswith("JF9SOM>JK4DHT-1,DP0SNX")


def test_position_frame_sets_command_bits_like_direwolf() -> None:
    frame = _build_aprs_position("JF9SOM", 3, "ARISS", 35.0, 139.0)
    assert frame[6] == 0xE0  # destination APRS-0
    assert frame[13] == 0xE0 | (3 << 1)  # source JF9SOM-3
    assert frame[20] == 0x61  # digipeater: H clear, last address


@pytest.mark.skipif(
    shutil.which("gen_packets") is None, reason="Direwolf gen_packets not installed"
)
@pytest.mark.parametrize(
    ("baud", "args"),
    [(1200, ["-B", "1200"]), (4800, ["-g", "-b", "4800"]), (9600, ["-B", "9600"])],
)
def test_audio_matches_direwolfs_gen_packets(baud: int, args: list[str], tmp_path: Path) -> None:
    text = tmp_path / "msg.txt"
    text.write_text("JF9SOM>APRS,ARISS::JA1ABC   :hello world\n")
    wav = tmp_path / "ref.wav"
    # -a 200 is full scale; gen_packets sends 32 flags first and 2 after the frame.
    subprocess.run(
        ["gen_packets", "-a", "200", "-r", "48000", *args, "-o", str(wav), str(text)],
        check=True,
        capture_output=True,
    )
    with wave.open(str(wav)) as w:
        ref = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2") / 32768.0
    ref = ref[np.nonzero(np.abs(ref) > 1e-4)[0][0] :]

    build = build_afsk_audio if baud == 1200 else build_g3ruh_audio
    mine = build(
        _DIREWOLF_FRAME,
        baud=baud,
        sample_rate=48_000,
        txdelay_ms=32 * 8000 / baud,
        txtail_ms=2 * 8000 / baud,
    ).astype(np.float64)

    # Direwolf rounds its phase through an 8-bit sine table and starts about one sample
    # late, so allow a small shift and a small RMS error rather than exact equality.
    best = min(
        np.sqrt(
            np.mean((ref[max(0, s) :][: len(mine) - 2] - mine[max(0, -s) :][: len(mine) - 2]) ** 2)
        )
        for s in range(-2, 3)
    )
    assert abs(len(ref) - len(mine)) <= 2
    assert best < 0.1


def test_parse_via_path_handles_ssid_lists_and_defaults() -> None:
    assert parse_via_path("ARISS") == [("ARISS", 0)]
    assert parse_via_path("W3ADO-1") == [("W3ADO", 1)]
    assert parse_via_path("WIDE1-1, WIDE2-1") == [("WIDE1", 1), ("WIDE2", 1)]
    assert parse_via_path("dp0snx*") == [("DP0SNX", 0)]
    assert parse_via_path("") == [("ARISS", 0)]
    assert parse_via_path("X-99") == [("X", 15)]
    assert len(parse_via_path(",".join(f"D{i}" for i in range(12)))) == 8


def test_via_ssid_and_several_digipeaters_reach_the_frame() -> None:
    frame = _build_aprs_message("JF9SOM", 0, "WIDE1-1,WIDE2-2", "JA1ABC", "hi")
    assert "JF9SOM>APRS,WIDE1-1,WIDE2-2 " in describe_ax25_frame(frame) + " "
    assert frame[20] == 0x60 | (1 << 1)  # first digipeater: not the last address
    assert frame[27] == 0x61 | (2 << 1)  # second digipeater: last address
