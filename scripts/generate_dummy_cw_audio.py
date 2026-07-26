#!/usr/bin/env python3
"""Generate a synthetic CW (Morse code) test WAV file for exercising the
Communications > CW Decoder tab (src/ui/cw_tab.py) without waiting for a
real satellite pass.

The generated file contains two short, unrelated "transmissions" separated
by a long silence (to exercise the newline-on-real-pause behaviour added in
comms/cw/transcript.py's insert_gap_markers()), plus one deliberately
long-but-not-that-long pause within the first transmission (to exercise the
plain-space case).

Usage:
    python scripts/generate_dummy_cw_audio.py [--wpm 20] [--out test_cw.wav]

Playback (Linux, PipeWire/PulseAudio):
    1. In Rig Settings > Sound Card, set the Input Device to
       "Monitor of <your normal output device>" (every sink has one of
       these by default — no virtual cable needed).
    2. Open Communications > CW Decoder, Input: Soundcard, press Start.
    3. Play the file through your normal speakers so it reaches the
       monitor source:
           paplay test_cw.wav
       (aplay talks to ALSA directly and may bypass the PipeWire monitor
       depending on your setup — prefer paplay/pw-play.)
"""

from __future__ import annotations

import argparse
import struct
import wave
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

_MORSE: dict[str, str] = {
    "A": ".-",
    "B": "-...",
    "C": "-.-.",
    "D": "-..",
    "E": ".",
    "F": "..-.",
    "G": "--.",
    "H": "....",
    "I": "..",
    "J": ".---",
    "K": "-.-",
    "L": ".-..",
    "M": "--",
    "N": "-.",
    "O": "---",
    "P": ".--.",
    "Q": "--.-",
    "R": ".-.",
    "S": "...",
    "T": "-",
    "U": "..-",
    "V": "...-",
    "W": ".--",
    "X": "-..-",
    "Y": "-.--",
    "Z": "--..",
    "0": "-----",
    "1": ".----",
    "2": "..---",
    "3": "...--",
    "4": "....-",
    "5": ".....",
    "6": "-....",
    "7": "--...",
    "8": "---..",
    "9": "----.",
    "/": "-..-.",
}


def _tone(
    duration_s: float, sample_rate: int, freq_hz: float, amplitude: float
) -> NDArray[np.float32]:
    """A sine-wave burst with a short raised-cosine ramp to avoid clicks."""
    n = max(int(round(duration_s * sample_rate)), 1)
    t = np.arange(n, dtype=np.float32) / sample_rate
    wave_samples = np.sin(2.0 * np.pi * freq_hz * t).astype(np.float32)

    ramp_n = min(n // 2, int(round(0.004 * sample_rate)))  # ~4 ms
    if ramp_n > 0:
        ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(ramp_n) / ramp_n)
        wave_samples[:ramp_n] *= ramp
        wave_samples[-ramp_n:] *= ramp[::-1]

    return wave_samples * amplitude


def _silence(duration_s: float, sample_rate: int) -> NDArray[np.float32]:
    n = max(int(round(duration_s * sample_rate)), 0)
    return np.zeros(n, dtype=np.float32)


def synthesize_message(
    text: str,
    wpm: float,
    sample_rate: int,
    freq_hz: float,
    amplitude: float = 0.6,
) -> NDArray[np.float32]:
    """Render *text* as CW audio at the given speed (standard, non-Farnsworth
    timing: dot = 1.2/wpm seconds, dash = 3 dots, intra-char gap = 1 dot,
    inter-char gap = 3 dots, inter-word gap = 7 dots)."""
    dot = 1.2 / wpm
    chunks: list[NDArray[np.float32]] = []
    prev_was_content = False

    for ch in text.upper():
        if ch == " ":
            chunks.append(_silence(dot * 7, sample_rate))
            prev_was_content = False
            continue
        pattern = _MORSE.get(ch)
        if pattern is None:
            continue
        if prev_was_content:
            chunks.append(_silence(dot * 3, sample_rate))
        for i, symbol in enumerate(pattern):
            if i > 0:
                chunks.append(_silence(dot, sample_rate))
            duration = dot if symbol == "." else dot * 3
            chunks.append(_tone(duration, sample_rate, freq_hz, amplitude))
        prev_was_content = True

    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks)


def build_test_audio(
    wpm: float, sample_rate: int, freq_hz: float, noise: float
) -> NDArray[np.float32]:
    """Two unrelated messages with a deliberate mid-message pause (should
    render as a single space) and a long gap between messages (should
    render as a newline)."""
    parts: list[NDArray[np.float32]] = [
        synthesize_message("CQ CQ", wpm, sample_rate, freq_hz),
        _silence(1.6, sample_rate),  # >=1s, <3s -> expect a plain space
        synthesize_message("DE JF9SOM K", wpm, sample_rate, freq_hz),
        _silence(8.0, sample_rate),  # >=3s -> expect a newline
        synthesize_message("TEST DE JF9SOM AR", wpm, sample_rate, freq_hz),
        _silence(6.0, sample_rate),  # let the final tail settle/confirm
    ]
    audio = np.concatenate(parts)

    if noise > 0.0:
        rng = np.random.default_rng(0)
        audio = audio + rng.normal(0.0, noise, size=audio.shape).astype(np.float32)

    return np.clip(audio, -1.0, 1.0)


def write_wav(path: Path, audio: NDArray[np.float32], sample_rate: int) -> None:
    pcm16 = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f"<{len(pcm16)}h", *pcm16.tolist()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wpm", type=float, default=20.0, help="CW speed in words per minute")
    parser.add_argument("--freq", type=float, default=700.0, help="Tone frequency in Hz")
    parser.add_argument("--sample-rate", type=int, default=48_000)
    parser.add_argument(
        "--noise", type=float, default=0.02, help="Background noise amplitude (0 to disable)"
    )
    parser.add_argument("--out", type=Path, default=Path("test_cw.wav"))
    args = parser.parse_args()

    audio = build_test_audio(args.wpm, args.sample_rate, args.freq, args.noise)
    write_wav(args.out, audio, args.sample_rate)
    duration_s = len(audio) / args.sample_rate
    print(f"Wrote {args.out} ({duration_s:.1f}s, {args.wpm:.0f} WPM, {args.freq:.0f} Hz)")


if __name__ == "__main__":
    main()
