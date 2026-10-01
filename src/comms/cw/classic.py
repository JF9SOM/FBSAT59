"""Classical (timing based) CW decoder, an alternative to the DeepCW model.

The model (comms.cw.codec) reads each character from a spectrogram and can
misread, drop or insert a character even on a strong, clean carrier (CW has no
error detection, so a single wrong hex digit ruins a telemetry frame). A strong
on/off-keyed carrier can also be read the old way: find the tone, take its
envelope, cut the envelope into marks and gaps, tell dits from dahs by length.

Returns the same :class:`~comms.cw.codec.DecodeResult` as the model, so the
transcript, the block extractor and the Telemetry tab need no special case.
numpy only (the CI has no scipy).
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from comms.cw.codec import HOP_LENGTH, SAMPLE_RATE, DecodeResult

# Tone search range in the demodulated audio, Hz.
_MIN_TONE_HZ = 300.0
_MAX_TONE_HZ = 3000.0
# The strongest spectral line must stand out of the median by this much to count as a carrier.
_MIN_TONE_SNR_DB = 10.0
# The envelope's keyed-on level must be at least this far above its off level.
_MIN_KEYING_RATIO = 2.0
# Envelope rate: fast enough for ~20 WPM keying (a dit is ~55 ms).
_ENV_RATE = 200.0
# Length of one analysis frame, seconds, and how far from the tone the carrier is followed, Hz.
_FRAME_S = 0.024
_TRACK_HZ = 75.0
# Spectrum resolution used to find the tone.
_TONE_FFT = 8192
_MIN_SECONDS = 3.0

_MORSE: dict[str, str] = {
    ".-": "A",
    "-...": "B",
    "-.-.": "C",
    "-..": "D",
    ".": "E",
    "..-.": "F",
    "--.": "G",
    "....": "H",
    "..": "I",
    ".---": "J",
    "-.-": "K",
    ".-..": "L",
    "--": "M",
    "-.": "N",
    "---": "O",
    ".--.": "P",
    "--.-": "Q",
    ".-.": "R",
    "...": "S",
    "-": "T",
    "..-": "U",
    "...-": "V",
    ".--": "W",
    "-..-": "X",
    "-.--": "Y",
    "--..": "Z",
    "-----": "0",
    ".----": "1",
    "..---": "2",
    "...--": "3",
    "....-": "4",
    ".....": "5",
    "-....": "6",
    "--...": "7",
    "---..": "8",
    "----.": "9",
    "--..--": ",",
    ".-.-.-": ".",
    "-..-.": "/",
    "..--..": "?",
}
# A pattern that is no Morse character (a mis-timed element): a marker the frame checks reject.
_UNKNOWN = "*"


def find_tone(audio: NDArray[np.float32], sample_rate: int) -> float | None:
    """Frequency (Hz) of the strongest narrow line in the CW band, or None if there is none."""
    n = min(len(audio), _TONE_FFT)
    if n < 1024:
        return None
    # Average the power spectrum over the window so a keyed carrier (on only part of the time)
    # still stands out.
    acc = np.zeros(n // 2 + 1)
    count = 0
    window = np.hanning(n)
    for start in range(0, len(audio) - n + 1, n // 2):
        spectrum = np.fft.rfft(audio[start : start + n].astype(np.float64) * window)
        acc += np.abs(spectrum) ** 2
        count += 1
    if count == 0:
        return None
    freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)
    band = (freqs >= _MIN_TONE_HZ) & (freqs <= _MAX_TONE_HZ)
    power = acc[band]
    if len(power) == 0:
        return None
    peak = int(np.argmax(power))
    median = float(np.median(power)) + 1e-30
    if 10.0 * np.log10(power[peak] / median + 1e-30) < _MIN_TONE_SNR_DB:
        return None
    return float(freqs[band][peak])


def _envelope(audio: NDArray[np.float32], sample_rate: int, tone_hz: float) -> NDArray[np.float64]:
    """Strength of the carrier near *tone_hz* over time, _ENV_RATE samples per second.

    A satellite's carrier wanders by tens of Hz, even within one key-down, so a fixed
    narrow filter would lose it in the middle of a mark. Each short frame therefore takes
    the strongest spectral component within _TRACK_HZ of *tone_hz*.
    """
    frame = int(round(_FRAME_S * sample_rate))
    hop = int(round(sample_rate / _ENV_RATE))
    if len(audio) < frame:
        return np.zeros(0)
    count = (len(audio) - frame) // hop + 1
    index = np.arange(frame)[None, :] + hop * np.arange(count)[:, None]
    frames = audio[index].astype(np.float64) * np.hanning(frame)[None, :]
    spectra = np.abs(np.fft.rfft(frames, axis=1))
    freqs = np.fft.rfftfreq(frame, 1.0 / sample_rate)
    band = np.abs(freqs - tone_hz) <= _TRACK_HZ
    if not band.any():
        band = np.abs(freqs - tone_hz) <= freqs[1]
    return np.asarray(spectra[:, band].max(axis=1))


def _runs(on: NDArray[np.bool_]) -> list[list[float]]:
    """[state, length_in_samples] runs of a boolean array."""
    runs: list[list[float]] = []
    if len(on) == 0:
        return runs
    state = bool(on[0])
    length = 0
    for value in on:
        if bool(value) == state:
            length += 1
        else:
            runs.append([1.0 if state else 0.0, float(length)])
            state = bool(value)
            length = 1
    runs.append([1.0 if state else 0.0, float(length)])
    return runs


def _two_means(values: NDArray[np.float64]) -> tuple[float, float] | None:
    """Centres of the two clusters of the 1-D *values*, or None if they do not split."""
    if len(values) < 4:
        return None
    low, high = float(np.percentile(values, 25)), float(np.percentile(values, 90))
    for _ in range(12):
        split = (low + high) / 2.0
        a, b = values[values < split], values[values >= split]
        if len(a) == 0 or len(b) == 0:
            return None
        low, high = float(a.mean()), float(b.mean())
    return low, high


def _estimate_unit(on_lengths: list[float]) -> tuple[float, float] | None:
    """(dit length, dit/dah boundary) in seconds from the keyed-on run lengths, or None."""
    values = np.array(sorted(v for v in on_lengths if v >= 0.025))
    centres = _two_means(values)
    if centres is not None and 2.2 <= centres[1] / max(centres[0], 1e-9) <= 4.5:
        low, high = centres
        return (low + high / 3.0) / 2.0, (low + high) / 2.0
    # All dits, or all dahs: keep the median only if it is plausible Morse timing.
    if len(values) < 4:
        return None
    median = float(np.median(values))
    return (median, 2.0 * median) if 0.03 <= median <= 0.25 else None


def _gap_limits(off_lengths: list[float], unit: float) -> tuple[float, float]:
    """(element/character, character/word) gap boundaries in seconds.

    Measured gaps run a little long (the envelope filter blurs the edges), so the
    boundaries come from where this signal's own gaps cluster; 2 and 5 units otherwise.
    """
    values = np.array([v for v in off_lengths if 0.4 * unit <= v < 4.5 * unit])
    centres = _two_means(values)
    if centres is not None and 1.8 <= centres[1] / max(centres[0], 1e-9) <= 5.0:
        return (centres[0] + centres[1]) / 2.0, centres[1] * 1.75
    return 2.0 * unit, 5.0 * unit


def _clean(runs: list[list[float]], min_len: float) -> list[list[float]]:
    """Merge runs shorter than *min_len* (keying chatter) into their neighbours."""
    changed = True
    while changed:
        changed = False
        for i, (_state, length) in enumerate(runs):
            if length >= min_len or len(runs) < 3 or i in (0, len(runs) - 1):
                continue
            # a short run flips to the surrounding state and the three runs become one
            runs[i - 1][1] += length + runs[i + 1][1]
            del runs[i : i + 2]
            changed = True
            break
    return runs


def decode_classic(audio: NDArray[np.float32], sample_rate: int) -> DecodeResult:
    """Decode *audio* (float32, mono) by keying timing. Empty result if there is no carrier."""
    empty = DecodeResult([], 0.0, np.zeros(0, dtype=np.float32))
    if len(audio) < _MIN_SECONDS * sample_rate:
        return empty
    duration = len(audio) / sample_rate
    tone = find_tone(audio, sample_rate)
    if tone is None:
        return DecodeResult([], duration, np.zeros(1, dtype=np.float32))
    env = _envelope(audio, sample_rate, tone)
    if len(env) < 8:
        return DecodeResult([], duration, np.zeros(1, dtype=np.float32))
    lo, hi = float(np.percentile(env, 5)), float(np.percentile(env, 97))
    frame_step = HOP_LENGTH / SAMPLE_RATE
    grid = np.arange(0.0, duration, frame_step)
    frame_energy = np.interp(grid, np.arange(len(env)) / _ENV_RATE + _FRAME_S / 2, env).astype(
        np.float32
    )
    if hi < _MIN_KEYING_RATIO * max(lo, 1e-12):
        return DecodeResult([], duration, frame_energy)

    # Schmitt trigger around the middle between the off and on levels.
    on = np.zeros(len(env), dtype=bool)
    rise, fall = lo + 0.5 * (hi - lo), lo + 0.3 * (hi - lo)
    state = False
    for i, value in enumerate(env):
        if not state and value > rise:
            state = True
        elif state and value < fall:
            state = False
        on[i] = state
    runs = _runs(on)
    estimate = _estimate_unit([r[1] / _ENV_RATE for r in runs if r[0] == 1.0])
    if estimate is None:
        return DecodeResult([], duration, frame_energy)
    unit, dash_limit = estimate
    runs = _clean(runs, 0.4 * unit * _ENV_RATE)
    char_gap, word_gap = _gap_limits([r[1] / _ENV_RATE for r in runs if r[0] == 0.0], unit)

    offsets: list[tuple[str, float]] = []
    symbol = ""
    position = 0.0  # seconds from the window start
    last_mark_end = 0.0
    for state_flag, length in runs:
        seconds = length / _ENV_RATE
        if state_flag == 1.0:
            symbol += "-" if seconds >= dash_limit else "."
            position += seconds
            last_mark_end = position
            continue
        position += seconds
        if seconds >= char_gap and symbol:
            offsets.append((_MORSE.get(symbol, _UNKNOWN), last_mark_end))
            symbol = ""
            if seconds >= word_gap:
                offsets.append((" ", last_mark_end + char_gap))
    # A character still open at the window's end is left out: it may continue in the next window.
    return DecodeResult(offsets, duration, frame_energy)
