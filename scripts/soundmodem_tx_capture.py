#!/usr/bin/env python3
"""Make UZ7HO SoundModem transmit one APRS frame, record the audio, and analyse it.

UISS is only a front end: the transmit audio of a UISS station is made by SoundModem.
This script drives SoundModem directly over its KISS TCP server (default port 8100),
so UISS is not needed, and compares the recorded audio with another recording
(for example FBSAT59's own transmit audio or Direwolf's gen_packets output).

Sub-commands
  send     Send "SRC>DEST,VIA:INFO" to SoundModem's KISS port (Windows, stdlib only).
  record   Record what the default output device plays, via WASAPI loopback
           (Windows, needs: pip install soundcard numpy).
  analyze  Print burst length, level and tone statistics of one or two WAV files
           (any OS, needs numpy).

Typical Windows session (SoundModem must be running, KISS server enabled, output device =
the speakers whose loopback is recorded, PTT = NONE, mode AFSK AX.25 1200bd):

  python soundmodem_tx_capture.py record --seconds 6 --out sm.wav
  (in a second window, within 2 s)  python soundmodem_tx_capture.py send
  python soundmodem_tx_capture.py analyze sm.wav other.wav
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
import wave
from typing import Any

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD


def _addr(call: str, ssid: int = 0, last: bool = False, cbit: bool = False) -> bytes:
    """One 7-byte AX.25 address field."""
    call = call.upper().ljust(6)[:6]
    out = bytearray(ord(c) << 1 for c in call)
    out.append(0x60 | (0x80 if cbit else 0) | ((ssid & 0x0F) << 1) | (1 if last else 0))
    return bytes(out)


def _split(call: str) -> tuple[str, int]:
    base, _sep, ssid = call.partition("-")
    return base, int(ssid) if ssid.isdigit() else 0


def build_frame(src: str, dest: str, via: str, info: str) -> bytes:
    """AX.25 UI frame without FCS (same layout as FBSAT59 / UISS: both C bits set)."""
    path = [v for v in via.replace(" ", "").split(",") if v]
    out = _addr(*_split(dest), cbit=True) + _addr(*_split(src), last=not path, cbit=True)
    for i, v in enumerate(path):
        out += _addr(*_split(v), last=i == len(path) - 1)
    return out + bytes([0x03, 0xF0]) + info.encode("ascii", errors="replace")


def kiss_wrap(frame: bytes, port: int = 0) -> bytes:
    """KISS data frame for *port*, with FEND/FESC escaping."""
    body = bytearray([(port & 0x0F) << 4])
    for b in frame:
        if b == FEND:
            body += bytes([FESC, TFEND])
        elif b == FESC:
            body += bytes([FESC, TFESC])
        else:
            body.append(b)
    return bytes([FEND]) + bytes(body) + bytes([FEND])


def cmd_send(args: argparse.Namespace) -> int:
    frame = build_frame(args.src, args.dest, args.via, args.info)
    data = kiss_wrap(frame, args.port)
    print(f"frame ({len(frame)} bytes): {frame.hex(' ')}")
    for n in range(args.count):
        with socket.create_connection((args.host, args.kiss_port), timeout=5) as s:
            s.sendall(data)
        print(f"sent {n + 1}/{args.count} to {args.host}:{args.kiss_port}")
        if n + 1 < args.count:
            time.sleep(args.interval)
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    try:
        import numpy as np
        import soundcard as sc
    except ImportError:
        print("record needs:  pip install soundcard numpy", file=sys.stderr)
        print(
            "Alternative: record the speakers with Audacity (Windows WASAPI loopback),",
            file=sys.stderr,
        )
        print("48000 Hz, mono, and save as WAV.", file=sys.stderr)
        return 2
    speaker = sc.default_speaker()
    mic = sc.get_microphone(speaker.name, include_loopback=True)
    print(f"recording loopback of: {speaker.name} for {args.seconds} s")
    with mic.recorder(samplerate=args.rate, channels=1) as rec:
        data = rec.record(numframes=int(args.rate * args.seconds))
    pcm = (np.clip(data[:, 0], -1, 1) * 32767).astype("<i2")
    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(args.rate)
        w.writeframes(pcm.tobytes())
    print(f"saved {args.out}")
    return 0


def _read_wav(path: str) -> tuple[Any, int]:
    import numpy as np

    with wave.open(path, "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise SystemExit(f"{path}: only 16-bit WAV is supported")
    x = np.frombuffer(raw, dtype="<i2").astype(float) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, rate


def _stats(path: str) -> dict[str, float]:
    import numpy as np

    x, rate = _read_wav(path)
    env = np.abs(x)
    peak = float(env.max())
    if peak <= 0:
        raise SystemExit(f"{path}: silent file")
    # Burst = from first to last sample above -30 dB of the peak (ignores quiet noise).
    idx = np.where(env > peak * 0.0316)[0]
    b = x[idx[0] : idx[-1] + 1]
    win = np.hanning(len(b))
    spec = np.abs(np.fft.rfft(b * win)) ** 2
    f = np.fft.rfftfreq(len(b), 1.0 / rate)
    total = spec.sum()

    def band(lo: float, hi: float) -> float:
        return float(spec[(f >= lo) & (f < hi)].sum())

    p1200, p2200 = band(1100, 1300), band(2100, 2300)
    return {
        "rate": float(rate),
        "burst_s": len(b) / rate,
        "peak_dbfs": 20 * float(np.log10(peak)),
        "rms_dbfs": 20 * float(np.log10(np.sqrt(np.mean(b**2)))),
        "p2200_minus_p1200_db": 10 * float(np.log10(p2200 / p1200)) if p1200 > 0 else float("nan"),
        "above_3k_pct": 100 * band(3000, rate / 2) / total,
        "above_4k_pct": 100 * band(4000, rate / 2) / total,
        "below_900_pct": 100 * band(0, 900) / total,
        "dc_offset": float(b.mean()),
    }


def cmd_analyze(args: argparse.Namespace) -> int:
    rows = [(p, _stats(p)) for p in args.files]
    names = [p.split("/")[-1].split("\\")[-1] for p, _ in rows]
    labels = [
        ("rate", "sample rate (Hz)"),
        ("burst_s", "burst length (s)"),
        ("peak_dbfs", "peak (dBFS)"),
        ("rms_dbfs", "rms (dBFS)"),
        ("p2200_minus_p1200_db", "2200 Hz vs 1200 Hz power (dB) [twist / pre-emphasis]"),
        ("above_3k_pct", "power above 3 kHz (%)"),
        ("above_4k_pct", "power above 4 kHz (%)"),
        ("below_900_pct", "power below 900 Hz (%)"),
        ("dc_offset", "DC offset"),
    ]
    print(f"{'':58}" + "".join(f"{n[:22]:>24}" for n in names))
    for key, text in labels:
        print(f"{text:58}" + "".join(f"{s[key]:>24.3f}" for _, s in rows))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="send one frame to SoundModem's KISS port")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--kiss-port", type=int, default=8100, help="SoundModem KISS server port")
    s.add_argument("--port", type=int, default=0, help="KISS channel (0 = ch A)")
    s.add_argument("--src", default="JF1PTU")
    s.add_argument("--dest", default="CQ")
    s.add_argument("--via", default="ARISS")
    s.add_argument("--info", default="PM95")
    s.add_argument("--count", type=int, default=1)
    s.add_argument("--interval", type=float, default=6.0)
    s.set_defaults(func=cmd_send)

    r = sub.add_parser("record", help="record the default output device (WASAPI loopback)")
    r.add_argument("--seconds", type=float, default=6.0)
    r.add_argument("--rate", type=int, default=48000)
    r.add_argument("--out", default="sm.wav")
    r.set_defaults(func=cmd_record)

    a = sub.add_parser("analyze", help="print burst/level/tone statistics of WAV files")
    a.add_argument("files", nargs="+")
    a.set_defaults(func=cmd_analyze)

    args = ap.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
