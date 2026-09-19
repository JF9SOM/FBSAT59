#!/usr/bin/env python3
"""Measure 9600 baud G3RUH AX.25 decode sensitivity of the SDR reception paths.

Two decoder paths are exercised with exactly the same 250 kHz complex IQ:

  direwolf      the app's G3ruhDiscriminator (src/comms/aprs/g3ruh_demod.py)
                feeding the real Direwolf binary (MODEM 9600), i.e. the chain
                the APRS / Telemetry tabs use for SDR reception.
  gr-satellites the gr_satellites command line on raw IQ (--iq), i.e. the
                Telemetry tab's gr-satellites mode.

Two sub-commands:

  sweep  Generate synthetic G3RUH frames in noise at a list of SNRs and report
         how many are decoded by each path. SNR is signal power divided by the
         noise power inside an 11 kHz band (the same definition used in
         docs/communications.md). Use this to check that a change to the demod
         chain did not cost sensitivity, or to find the decode threshold.
  file   Run a real IQ recording (SDR Control tab "IQ Record" WAV, float32 or
         int16 stereo, or a raw complex64 .cf32 file) through both paths. Use
         --start / --duration (seconds) to cut a section out of a long file.

Usage:
    python scripts/g3ruh_sensitivity.py sweep --snr 20 16 14 12 10
    python scripts/g3ruh_sensitivity.py sweep --snr 16 --offset-hz 2000
    python scripts/g3ruh_sensitivity.py file ~/iq_recordings/x.iq.wav --start 300 --duration 320

Reference results (2026-09-19, 40 frames per SNR, 180 byte info field, +/-2.4 kHz
deviation, BT 0.5): the Direwolf chain reaches 50% at about 12.5 dB and
gr-satellites at about 14 dB. Both tolerate +/-2 kHz frequency error and fail at
+/-4 kHz. The generator is an ideal GFSK signal, so real satellites can be
somewhat worse; treat the numbers as an upper bound on sensitivity.

Direwolf's KISS/AGW ports are disabled here, so this can run while the app is
open. Direwolf silently ignores a -c path of roughly 100 characters or more, so
it is always started with a short relative config name inside a temp directory.
"""

from __future__ import annotations

import argparse
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from scipy import signal

FS = 250_000
BAUD = 9_600
FRAME_TAG = "GRBBeta test frame"
_SRC = Path(__file__).resolve().parent.parent / "src"
_DIREWOLF_CONF = (
    "MYCALL N0CALL\n"
    "ADEVICE stdin stdout\n"
    "ARATE 48000\n"
    "ACHANNELS 1\n"
    "CHANNEL 0\n"
    "MODEM 9600\n"
    "PTT NONE\n"
    "KISSPORT 0\n"
    "AGWPORT 0\n"
)


# ---------------------------------------------------------------------------
# Synthetic signal generation
# ---------------------------------------------------------------------------


def crc16_x25(data: bytes) -> int:
    """CRC-16/X.25 as used for the AX.25 frame check sequence."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def _ax25_address(call: str, last: bool) -> bytes:
    raw = call.ljust(6)[:6].encode()
    return bytes(b << 1 for b in raw) + bytes([0x60 | (1 if last else 0)])


def ax25_frame(info: bytes) -> bytes:
    """Build an AX.25 UI frame (destination TLMGRB, source GRBBET) with FCS."""
    body = _ax25_address("TLMGRB", False) + _ax25_address("GRBBET", True) + b"\x03\xf0" + info
    fcs = crc16_x25(body)
    return body + bytes([fcs & 0xFF, fcs >> 8])


def _bits_lsb_first(data: bytes) -> list[int]:
    return [(b >> i) & 1 for b in data for i in range(8)]


def hdlc_bits(frame: bytes, preamble_flags: int, trailing_flags: int = 4) -> list[int]:
    """HDLC-frame ``frame``: leading flags, bit-stuffed body, trailing flags."""
    flag = _bits_lsb_first(b"\x7e")
    out = flag * preamble_flags
    ones = 0
    for bit in _bits_lsb_first(frame):
        out.append(bit)
        if bit:
            ones += 1
            if ones == 5:
                out.append(0)
                ones = 0
        else:
            ones = 0
    return out + flag * trailing_flags


def nrzi_encode(bits: list[int]) -> list[int]:
    """NRZI as used by AX.25: a 0 toggles the level, a 1 keeps it."""
    out: list[int] = []
    level = 0
    for bit in bits:
        if bit == 0:
            level ^= 1
        out.append(level)
    return out


def g3ruh_scramble(bits: list[int]) -> list[int]:
    """G3RUH self-synchronising scrambler, polynomial 1 + x^12 + x^17."""
    lfsr = 0
    out: list[int] = []
    for bit in bits:
        x = (bit ^ (lfsr >> 16) ^ (lfsr >> 11)) & 1
        lfsr = ((lfsr << 1) | x) & 0x1FFFF
        out.append(x)
    return out


def burst_iq(
    info: bytes, deviation_hz: float = 2400.0, bt: float = 0.5, preamble_flags: int = 60
) -> np.ndarray:
    """Return one Gaussian-filtered FSK burst as unit-power complex64 IQ at 250 kHz."""
    bits = g3ruh_scramble(nrzi_encode(hdlc_bits(ax25_frame(info), preamble_flags)))
    sps = 25  # 240 kHz internal rate, resampled 25/24 to 250 kHz below
    rate = BAUD * sps
    nrz = np.repeat(np.array(bits, dtype=np.float64) * 2.0 - 1.0, sps)
    padded = np.concatenate([np.full(sps * 4, nrz[0]), nrz, np.full(sps * 4, nrz[-1])])
    sigma = np.sqrt(np.log(2)) / (2 * np.pi * bt) * sps
    taps = int(sigma * 8) | 1
    t = np.arange(taps) - taps // 2
    gauss = np.exp(-(t**2) / (2 * sigma**2))
    gauss /= gauss.sum()
    shaped = np.convolve(padded, gauss, mode="same")
    phase = 2 * np.pi * np.cumsum(shaped) * deviation_hz / rate
    iq = np.exp(1j * phase).astype(np.complex64)
    return np.asarray(signal.resample_poly(iq, 25, 24), dtype=np.complex64)


def make_stream(
    snr_db: float,
    frames: int,
    freq_offset_hz: float = 0.0,
    preamble_flags: int = 60,
    info_len: int = 180,
    seed: int = 1,
) -> np.ndarray:
    """Return ``frames`` bursts separated by noise. SNR is measured in an 11 kHz band."""
    rng = np.random.default_rng(seed)
    noise_power = FS / (11_000.0 * 10 ** (snr_db / 10))  # per-sample, signal power is 1

    def noise(n: int) -> np.ndarray:
        scale = np.sqrt(noise_power / 2)
        return ((rng.standard_normal(n) + 1j * rng.standard_normal(n)) * scale).astype(np.complex64)

    parts = [noise(int(0.5 * FS))]
    guard = np.zeros(int(0.05 * FS), np.complex64)
    for k in range(frames):
        text = f"{FRAME_TAG} {k:03d} ".encode()
        info = text + bytes(rng.integers(32, 127, info_len - len(text), dtype=np.uint8))
        burst = burst_iq(info, preamble_flags=preamble_flags)
        if freq_offset_hz:
            burst = burst * np.exp(2j * np.pi * freq_offset_hz * np.arange(len(burst)) / FS).astype(
                np.complex64
            )
        seg = np.concatenate([guard, burst, guard])
        parts.append(seg + noise(len(seg)))
        parts.append(noise(int(0.4 * FS)))
    return np.concatenate(parts).astype(np.complex64)


# ---------------------------------------------------------------------------
# Real recording input
# ---------------------------------------------------------------------------


def read_iq(path: Path, start_s: float, duration_s: float | None) -> np.ndarray:
    """Read complex64 IQ at 250 kHz from a WAV (float32/int16 stereo) or raw .cf32 file."""
    offset = 0
    kind = "cf32"
    if path.suffix.lower() == ".wav":
        with path.open("rb") as f:
            if f.read(4) != b"RIFF":
                raise SystemExit(f"{path}: not a RIFF/WAV file")
            f.seek(8)
            if f.read(4) != b"WAVE":
                raise SystemExit(f"{path}: not a WAVE file")
            fmt: tuple[int, int, int] | None = None
            while True:
                head = f.read(8)
                if len(head) < 8:
                    raise SystemExit(f"{path}: no data chunk")
                cid, size = struct.unpack("<4sI", head)
                if cid == b"fmt ":
                    tag, chans, rate = struct.unpack("<HHI", f.read(8))
                    f.seek(size - 8, 1)
                    fmt = (tag, chans, rate)
                elif cid == b"data":
                    offset = f.tell()
                    break
                else:
                    f.seek(size + (size & 1), 1)
        if fmt is None or fmt[1] != 2:
            raise SystemExit(f"{path}: expected a 2-channel (I/Q) WAV")
        if fmt[2] != FS:
            raise SystemExit(f"{path}: sample rate is {fmt[2]} Hz, this tool needs {FS} Hz")
        kind = "f32" if fmt[0] == 3 else "i16"

    bytes_per_iq = {"cf32": 8, "f32": 8, "i16": 4}[kind]
    first = int(start_s * FS)
    count = -1 if duration_s is None else int(duration_s * FS)
    with path.open("rb") as f:
        f.seek(offset + first * bytes_per_iq)
        if kind == "i16":
            raw = np.fromfile(f, dtype=np.int16, count=-1 if count < 0 else count * 2)
            return (raw[0::2] / 32768.0 + 1j * raw[1::2] / 32768.0).astype(np.complex64)
        return np.fromfile(f, dtype=np.complex64, count=count)


# ---------------------------------------------------------------------------
# Decoder paths
# ---------------------------------------------------------------------------


def run_direwolf(iq: np.ndarray) -> tuple[int, list[str]]:
    """Run the app's G3RUH discriminator + Direwolf. Returns (frame count, frame lines)."""
    sys.path.insert(0, str(_SRC))
    from comms.aprs.direwolf import find_direwolf
    from comms.aprs.g3ruh_demod import G3ruhDiscriminator

    binary = find_direwolf()
    if binary is None:
        raise RuntimeError("Direwolf not found (Help > Direwolf... installs it)")
    disc = G3ruhDiscriminator(input_rate=FS, baud=BAUD)
    block = 16384  # the SDRPipeline block size
    chunks = [disc.process(iq[i : i + block]) for i in range(0, len(iq), block)]
    pcm = np.concatenate([c for c in chunks if len(c)])
    data = (np.clip(pcm, -1.0, 1.0) * 32767).astype("int16").tobytes()
    with tempfile.TemporaryDirectory(prefix="dw_") as tmp:
        (Path(tmp) / "dw.conf").write_text(_DIREWOLF_CONF)
        proc = subprocess.run(
            [str(binary), "-c", "dw.conf", "-t", "0"],
            cwd=tmp,
            input=data,
            capture_output=True,
            timeout=1800,
        )
    text = proc.stdout.decode(errors="replace")
    lines = [ln for ln in text.splitlines() if re.match(r"^\[\d", ln)]
    return len(lines), lines


def run_gr_satellites(iq: np.ndarray) -> tuple[int, list[bytes]]:
    """Run gr_satellites on raw IQ. Returns (frame count, decoded PDUs)."""
    sys.path.insert(0, str(_SRC))
    from comms.telemetry.gr_satellites_install import resolve_gr_satellites_command

    resolved = resolve_gr_satellites_command()
    if resolved is None:
        raise RuntimeError("gr_satellites not found (Help > gr-satellites Installation)")
    argv, _ = resolved
    with tempfile.TemporaryDirectory(prefix="gr_") as tmp:
        iq.astype(np.complex64).tofile(Path(tmp) / "in.cf32")
        proc = subprocess.run(
            [*argv, "60237", "--rawfile", "in.cf32", "--samp_rate", str(FS), "--iq", "--hexdump"],
            cwd=tmp,
            capture_output=True,
            timeout=3600,
        )
    text = (proc.stdout + proc.stderr).decode(errors="replace")
    pdus: list[bytes] = []
    for block in text.split("VERBOSE PDU DEBUG PRINT")[1:]:
        hexes = re.findall(r"^[0-9a-f]{4}: ((?:[0-9a-f]{2} ?)+)", block, re.MULTILINE)
        pdus.append(bytes.fromhex("".join(hexes).replace(" ", "")))
    return len(pdus), pdus


def _synthetic_hits_direwolf(lines: list[str]) -> int:
    return len({m for ln in lines for m in re.findall(rf"{FRAME_TAG} (\d{{3}})", ln)})


def _synthetic_hits_gr(pdus: list[bytes]) -> int:
    ids = set()
    for pdu in pdus:
        m = re.search(rf"{FRAME_TAG} (\d{{3}})".encode(), pdu)
        if m:
            ids.add(m.group(1))
    return len(ids)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _cmd_sweep(args: argparse.Namespace) -> int:
    frames = args.frames
    print(
        f"{frames} frames per SNR, offset {args.offset_hz:+.0f} Hz, "
        f"preamble {args.preamble_flags} flags"
    )
    for snr in args.snr:
        iq = make_stream(snr, frames, args.offset_hz, args.preamble_flags)
        cells = []
        if "direwolf" in args.decoders:
            _, lines = run_direwolf(iq)
            cells.append(f"Direwolf {_synthetic_hits_direwolf(lines):2d}/{frames}")
        if "gr-satellites" in args.decoders:
            _, pdus = run_gr_satellites(iq)
            cells.append(f"gr-satellites {_synthetic_hits_gr(pdus):2d}/{frames}")
        print(f"SNR(11 kHz) {snr:5.1f} dB : " + "   ".join(cells), flush=True)
    return 0


def _cmd_file(args: argparse.Namespace) -> int:
    iq = read_iq(args.path, args.start, args.duration)
    print(f"{args.path.name}: {len(iq) / FS:.1f} s of IQ from t={args.start:.1f} s")
    if "direwolf" in args.decoders:
        n, lines = run_direwolf(iq)
        print(f"Direwolf      : {n} frame(s)")
        for ln in lines[:20]:
            print("   ", ln[:140])
    if "gr-satellites" in args.decoders:
        n, pdus = run_gr_satellites(iq)
        print(f"gr-satellites : {n} frame(s)")
        for pdu in pdus[:20]:
            print("   ", pdu[:48].hex(" "))
    return 0


def main() -> int:
    """Parse arguments and run the chosen sub-command."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--decoders",
            nargs="+",
            choices=["direwolf", "gr-satellites"],
            default=["direwolf", "gr-satellites"],
            help="which decoder paths to run (default: both)",
        )

    sweep = sub.add_parser("sweep", help="synthetic frames at a list of SNRs")
    sweep.add_argument("--snr", type=float, nargs="+", default=[20, 16, 14, 13, 12, 11, 10, 8])
    sweep.add_argument("--frames", type=int, default=40)
    sweep.add_argument("--offset-hz", type=float, default=0.0, help="carrier frequency error")
    sweep.add_argument("--preamble-flags", type=int, default=60, help="leading 0x7E flags")
    add_common(sweep)
    sweep.set_defaults(func=_cmd_sweep)

    file_ = sub.add_parser("file", help="run a real IQ recording through the decoders")
    file_.add_argument("path", type=Path)
    file_.add_argument("--start", type=float, default=0.0, help="start offset in seconds")
    file_.add_argument("--duration", type=float, default=None, help="length in seconds")
    add_common(file_)
    file_.set_defaults(func=_cmd_file)

    args = parser.parse_args()
    try:
        return int(args.func(args))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
