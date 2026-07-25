"""CW decoder inference using the DeepCW ONNX model (e04/deepcw-engine).

Preprocessing parameters are taken from model.onnx.json:
  sample_rate             = 3 200 Hz
  fft_length              = 256
  hop_length              = 48
  spectrogram_min_freq_hz = 400.0
  spectrogram_max_freq_hz = 1 200.0
  spectrogram_bins        = 65
  normalization           = "log1p"
  blank_index             = 41

The Python preprocessing exactly mirrors examples/python/decode_morse.py
from the deepcw-engine repository.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

from comms.cw.model_info import find_model

if TYPE_CHECKING:
    import onnxruntime as ort

# ---------------------------------------------------------------------------
# Constants (from model.onnx.json)
# ---------------------------------------------------------------------------

SAMPLE_RATE = 3_200  # Hz — model's native sample rate
FFT_LENGTH = 256
HOP_LENGTH = 48
_MIN_HZ = 400.0
_MAX_HZ = 1_200.0
_EXPECTED_BINS = 65
_BLANK_INDEX = 41

MIN_AUDIO_SECONDS = 5.0  # model requires at least 5 s of audio
MAX_AUDIO_SECONDS = 20.0  # clip to 20 s maximum

# Vocabulary (chars list from model.onnx.json, blank implicit at index 41)
_VOCAB: list[str] = list(",./0123456789?ABCDEFGHIJKLMNOPQRSTUVWXYZ ")

# Hann window matching the official Python example: hanning(N+1)[:-1]
_HANN: NDArray[np.float32] = np.hanning(FFT_LENGTH + 1)[:-1].astype(np.float32)

# Frequency bin range [_START_BIN, _STOP_BIN)
_BIN_HZ = SAMPLE_RATE / FFT_LENGTH  # 12.5 Hz per bin
_START_BIN = int(math.ceil(_MIN_HZ / _BIN_HZ))  # 32
_STOP_BIN = int(math.floor(_MAX_HZ / _BIN_HZ)) + 1  # 97


# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------


def _resample(audio: NDArray[np.float32], src_rate: int) -> NDArray[np.float32]:
    """Resample *audio* from *src_rate* to SAMPLE_RATE (3200 Hz)."""
    if src_rate == SAMPLE_RATE:
        return audio
    try:
        from math import gcd

        from scipy.signal import resample_poly

        g = gcd(SAMPLE_RATE, src_rate)
        up, down = SAMPLE_RATE // g, src_rate // g
        out: NDArray[np.float32] = resample_poly(audio, up, down).astype(np.float32)
        return out
    except ImportError:
        # Linear interpolation fallback (no scipy)
        target_length = int(round(len(audio) * SAMPLE_RATE / src_rate))
        src_pos = np.arange(target_length, dtype=np.float64) * src_rate / SAMPLE_RATE
        left = np.floor(src_pos).astype(np.int64)
        right = np.minimum(left + 1, len(audio) - 1)
        frac = (src_pos - left).astype(np.float32)
        out = audio[left] * (1.0 - frac) + audio[right] * frac
        return out.astype(np.float32)


# ---------------------------------------------------------------------------
# Spectrogram
# ---------------------------------------------------------------------------


def _compute_spectrogram(audio: NDArray[np.float32]) -> NDArray[np.float32] | None:
    """Compute log1p magnitude spectrogram matching the deepcw-engine pipeline.

    Returns float32 array of shape (1, 1, time_steps, _EXPECTED_BINS),
    or None if the audio is outside the valid duration range.
    """
    duration = len(audio) / SAMPLE_RATE
    if duration < MIN_AUDIO_SECONDS:
        return None
    # Clip to maximum
    max_samples = int(MAX_AUDIO_SECONDS * SAMPLE_RATE)
    if len(audio) > max_samples:
        audio = audio[:max_samples]

    # Reflect-pad by fft_length // 2 on each side
    pad = FFT_LENGTH // 2
    padded = np.pad(audio, (pad, pad), mode="reflect")

    n_frames = 1 + (len(padded) - FFT_LENGTH) // HOP_LENGTH
    spectrogram = np.empty((n_frames, _EXPECTED_BINS), dtype=np.float32)

    for i in range(n_frames):
        start = i * HOP_LENGTH
        frame = padded[start : start + FFT_LENGTH] * _HANN
        spectrum = np.abs(np.fft.rfft(frame, n=FFT_LENGTH))
        spectrogram[i] = spectrum[_START_BIN:_STOP_BIN].astype(np.float32)

    # log1p normalization
    spectrogram = np.log1p(spectrogram, dtype=np.float32)

    return spectrogram[np.newaxis, np.newaxis, :, :].astype(np.float32)


# ---------------------------------------------------------------------------
# CTC greedy decoder
# ---------------------------------------------------------------------------


def _ctc_decode_with_offsets(log_probs: NDArray[np.float32]) -> list[tuple[str, float]]:
    """Greedy CTC decode returning (char, seconds_from_window_start) pairs.

    Matches greedy_ctc_decode() in examples/python/decode_morse.py:
    blank resets previous label (no space output); repeated labels collapse.
    The offset lets callers judge how close to the trailing edge of the
    audio window (i.e. how little right-context the model had) a given
    character is — useful for deciding whether a reading is stable enough
    to treat as final.
    """
    best_path = log_probs[0].argmax(axis=-1)
    decoded: list[tuple[str, float]] = []
    previous: int | None = None

    for frame_idx, idx in enumerate(best_path):
        i = int(idx)
        if i == _BLANK_INDEX:
            previous = None
            continue
        if i != previous:
            ch = _VOCAB[i] if i < len(_VOCAB) else ""
            decoded.append((ch, frame_idx * HOP_LENGTH / SAMPLE_RATE))
        previous = i

    return decoded


def _ctc_decode(log_probs: NDArray[np.float32]) -> str:
    """Greedy CTC decode of shape (1, time_steps, num_classes) → text."""
    return "".join(ch for ch, _t in _ctc_decode_with_offsets(log_probs))


# ---------------------------------------------------------------------------
# CwDecoder
# ---------------------------------------------------------------------------


class CwDecoder:
    """Wraps the DeepCW ONNX model for inference.

    Usage::

        decoder = CwDecoder()
        text = decoder.decode(audio_float32, sample_rate=48000)
    """

    def __init__(self) -> None:
        self._session: ort.InferenceSession | None = None
        self._load()

    def _load(self) -> None:
        model_path = find_model()
        if model_path is None:
            return
        try:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            self._session = ort.InferenceSession(
                str(model_path),
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
        except Exception:
            self._session = None

    @property
    def is_ready(self) -> bool:
        """Return True if the model is loaded and ready."""
        return self._session is not None

    def decode_with_offsets(
        self, audio: NDArray[np.float32], sample_rate: int
    ) -> tuple[list[tuple[str, float]], float]:
        """Decode *audio* and return (char, offset_seconds) pairs plus the
        actual window duration (seconds) used for inference.

        The offset for each character is measured from the start of the
        resampled window (clipped to MAX_AUDIO_SECONDS), so
        ``window_duration - offset`` is how much trailing audio followed
        that character — how much right-context the model had when it
        produced it. Returns ``([], 0.0)`` if the model isn't loaded or the
        audio is shorter than MIN_AUDIO_SECONDS, matching decode()'s ""
        behaviour.
        """
        if self._session is None:
            return [], 0.0

        resampled = _resample(audio, sample_rate)
        spec = _compute_spectrogram(resampled)
        if spec is None:
            return [], 0.0

        max_samples = int(MAX_AUDIO_SECONDS * SAMPLE_RATE)
        window_duration = min(len(resampled), max_samples) / SAMPLE_RATE

        input_name = "spectrogram"
        output_name = "log_probs"
        outputs = self._session.run([output_name], {input_name: spec})
        return _ctc_decode_with_offsets(outputs[0]), window_duration

    def decode(self, audio: NDArray[np.float32], sample_rate: int) -> str:
        """Decode *audio* (float32, mono) and return the CW text.

        Returns an empty string if the model is not loaded or the audio
        is too short (< MIN_AUDIO_SECONDS after resampling).
        """
        offsets, _duration = self.decode_with_offsets(audio, sample_rate)
        return "".join(ch for ch, _t in offsets)

    def reload(self) -> None:
        """Reload the model from disk (e.g. after installation)."""
        self._session = None
        self._load()
