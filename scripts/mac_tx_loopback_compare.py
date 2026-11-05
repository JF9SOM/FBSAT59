#!/usr/bin/env python3
"""Record FBSAT59's and Direwolf's real transmit playback on macOS and compare them.

Both audio sources are played to the same loopback device (default: "VB-Cable") and
recorded from it, so the whole playback path (PortAudio/CoreAudio output, level, start
and end of the burst) is captured, not only the generated samples.

  FBSAT59   comms.aprs.ax25_tx.build_ax25_audio() played with sounddevice.play(),
            the same call PttAudioTxWorker uses.
  Direwolf  `direwolf` started with ``ADEVICE stdin <device>`` (RX audio = silence),
            one KISS frame sent to its KISS TCP port; Direwolf plays the TX audio.

Needs microphone permission for the process running it (the loopback is an input
device); without it the recording is all zeros and the script says so.

  python scripts/mac_tx_loopback_compare.py [--device "VB-Cable"] [--out-dir DIR]
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402
import soundmodem_tx_capture as cap  # noqa: E402

RATE = 48_000
SRC, DEST, VIA, INFO = "JF1PTU", "CQ", "ARISS", "PM95"


def _find_device(name: str) -> int:
    for i, d in enumerate(sd.query_devices()):
        if (
            name.lower() in d["name"].lower()
            and d["max_input_channels"]
            and d["max_output_channels"]
        ):
            return i
    raise SystemExit(f"no input+output audio device matching {name!r}")


def _avf_index(name: str) -> int:
    """ffmpeg avfoundation audio-device index of *name*."""
    import re

    out = subprocess.run(
        ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True,
        text=True,
    ).stderr
    audio = out.split("AVFoundation audio devices:")[-1]
    for m in re.finditer(r"\[(\d+)\] (.+)", audio):
        if name.lower() in m.group(2).lower():
            return int(m.group(1))
    raise SystemExit(f"ffmpeg has no audio input matching {name!r}")


_AVF_NAME = "VB-Cable"
_DW_ADEVICE = ""


def _record(_device: int, seconds: float, holder: dict[str, Any]) -> None:
    """Record the loopback with ffmpeg in a separate process.

    PortAudio cannot open an input and an output stream on the same CoreAudio device
    in one process (AUHAL error -10863), so the recorder lives in its own process.
    """
    import soundfile as sf

    path = Path(tempfile.gettempdir()) / "tx_compare_rec.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "avfoundation",
            "-i",
            f":{_avf_index(_AVF_NAME)}",
            "-t",
            str(seconds),
            "-ar",
            str(RATE),
            "-ac",
            "2",
            "-c:a",
            "pcm_f32le",
            str(path),
        ],
        check=False,
    )
    data, _ = sf.read(str(path), dtype="float32", always_2d=True)
    holder["data"] = data


def _save_trimmed(data: Any, path: Path) -> float:
    """Save the burst (with 0.3 s margins) as mono 16-bit WAV; returns the peak."""
    x = data.mean(axis=1)
    x[: int(0.3 * RATE)] = 0.0  # ffmpeg start-up click
    peak = float(np.abs(x).max())
    if peak < 1e-4:
        return peak
    idx = np.where(np.abs(x) > peak * 0.0316)[0]
    m = int(0.3 * RATE)
    seg = x[max(0, idx[0] - m) : idx[-1] + m]
    cap._write_wav(str(path), (np.clip(seg, -1, 1) * 32767).astype("<i2"), RATE)
    return peak


def _fbsat59_audio() -> Any:
    from comms.aprs.ax25_tx import build_ax25_audio
    from comms.aprs.engine import _build_uiss_message

    frame = _build_uiss_message(SRC, 0, VIA, DEST, INFO)
    return (np.asarray(build_ax25_audio(frame, "1200"), dtype="float32") * 0.5).astype("float32")


def _run_fbsat59(device: int, seconds: float) -> Any:
    holder: dict[str, Any] = {}
    t = threading.Thread(target=_record, args=(device, seconds, holder))
    t.start()
    time.sleep(1.0)
    sd.play(_fbsat59_audio(), samplerate=RATE, device=device, blocking=False)
    sd.wait()
    t.join()
    return holder["data"]


def _run_direwolf(device: int, seconds: float, tmp: Path) -> Any:
    conf = tmp / "t.conf"
    conf.write_text(
        f"ADEVICE {_DW_ADEVICE or f'stdin {device}'}\nACHANNELS 1\nCHANNEL 0\n"
        f"MYCALL {SRC}\nMODEM 1200\nKISSPORT 8011\n"
    )
    holder: dict[str, Any] = {}
    dw = subprocess.Popen(
        ["direwolf", "-c", "t.conf", "-t", "0"],
        cwd=tmp,
        stdin=subprocess.PIPE,
        stdout=(tmp / "direwolf.log").open("w"),
        stderr=subprocess.STDOUT,
    )

    def _silence() -> None:
        chunk = bytes(2 * RATE // 10)
        try:
            while dw.poll() is None:
                dw.stdin.write(chunk)  # type: ignore[union-attr]
                dw.stdin.flush()  # type: ignore[union-attr]
                time.sleep(0.1)
        except (BrokenPipeError, OSError, ValueError):
            pass

    feeder = threading.Thread(target=_silence, daemon=True)
    feeder.start()
    try:
        time.sleep(2.0)
        t = threading.Thread(target=_record, args=(device, seconds, holder))
        t.start()
        time.sleep(3.0)
        frame = cap.build_frame(SRC, DEST, VIA, INFO)
        with socket.create_connection(("127.0.0.1", 8011), timeout=5) as s:
            s.sendall(cap.kiss_wrap(frame))
        t.join()
    finally:
        dw.terminate()
        try:
            dw.wait(timeout=3)
        except subprocess.TimeoutExpired:
            dw.kill()
    return holder["data"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", default="VB-Cable")
    ap.add_argument(
        "--dw-adevice", default="", help='whole ADEVICE value, e.g. "4 10" (default: stdin <dev>)'
    )
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--out-dir", default=str(Path(tempfile.gettempdir()) / "tx_compare"))
    args = ap.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    dev = _find_device(args.device)
    global _AVF_NAME, _DW_ADEVICE
    _DW_ADEVICE = args.dw_adevice
    _AVF_NAME = args.device
    print(f"loopback device: {dev} {sd.query_devices(dev)['name']}")

    results: list[str] = []
    for name, fn in (
        ("fbsat59", lambda: _run_fbsat59(dev, args.seconds)),
        ("direwolf", lambda: _run_direwolf(dev, args.seconds + 2, out)),
    ):
        data = fn()
        path = out / f"{name}_loop.wav"
        peak = _save_trimmed(data, path)
        if peak < 1e-4:
            print(f"{name}: recording is silent (microphone permission for this process?)")
            continue
        print(f"{name}: saved {path} (raw peak {peak:.4f})")
        results.append(str(path))
    if len(results) >= 1:
        table = cap._table(results)
        print(table)
        (out / "compare.txt").write_text(table + "\n")
        print(f"saved {out / 'compare.txt'}")
    return 0 if len(results) == 2 else 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
