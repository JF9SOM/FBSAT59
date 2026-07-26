#!/usr/bin/env python3
"""Generate a synthetic CW (Morse code) test WAV file for exercising the
Communications > CW Decoder tab (src/ui/cw_tab.py) without waiting for a
real satellite pass.

The generated file renders a short mock QSO (--repeat times) with a mix of
gap lengths: a short mid-message pause (should render as a plain space), a
medium pause (still a space), and long pauses between exchanges/repeats
(should render as a newline — see comms/cw/transcript.py's
insert_gap_markers()).

Usage:
    python scripts/generate_dummy_cw_audio.py [--wpm 20] [--repeat 1] [--out test_cw.wav]

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
    "?": "..--..",
    ".": ".-.-.-",
    ",": "--..--",
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


# One mock QSO exchange: (message, silence_after_s) pairs. Silences mix
# short (<1s, no marker), medium (>=1s <3s -> space) and long (>=3s ->
# newline) gaps to exercise all three cases in every repeat.
_QSO_LINES: list[tuple[str, float]] = [
    ("CQ CQ CQ DE JF9SOM JF9SOM K", 1.6),  # mid: plain space
    ("DE JA1XYZ JA1XYZ K", 8.0),  # long: newline (end of exchange)
    ("JA1XYZ DE JF9SOM UR 599 599 BT NAME IS TARO TARO BT QTH TOKYO TOKYO BT HW?", 0.3),
    ("AR", 2.2),  # medium: plain space
    ("JF9SOM DE JA1XYZ R R 599 599 TU 73 SK", 10.0),  # long: newline
]


def build_test_audio(
    wpm: float,
    sample_rate: int,
    freq_hz: float,
    noise: float,
    repeat: int = 1,
) -> NDArray[np.float32]:
    """Render the mock QSO in _QSO_LINES *repeat* times, with an extra long
    pause between repeats so each run is clearly its own transmission."""
    parts: list[NDArray[np.float32]] = []
    for rep in range(repeat):
        for text, silence_after in _QSO_LINES:
            parts.append(synthesize_message(text, wpm, sample_rate, freq_hz))
            parts.append(_silence(silence_after, sample_rate))
        if rep < repeat - 1:
            parts.append(_silence(12.0, sample_rate))  # long: newline between repeats
    parts.append(_silence(6.0, sample_rate))  # let the final tail settle/confirm

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
    parser.add_argument(
        "--repeat", type=int, default=1, help="Repeat the mock QSO this many times (longer file)"
    )
    parser.add_argument("--out", type=Path, default=Path("test_cw.wav"))
    args = parser.parse_args()

    audio = build_test_audio(args.wpm, args.sample_rate, args.freq, args.noise, args.repeat)
    write_wav(args.out, audio, args.sample_rate)
    duration_s = len(audio) / args.sample_rate
    print(
        f"Wrote {args.out} ({duration_s:.1f}s, {args.wpm:.0f} WPM, "
        f"{args.freq:.0f} Hz, x{args.repeat} repeat)"
    )


if __name__ == "__main__":
    main()
