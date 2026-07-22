"""FT4 codec — wraps ft8_lib via ctypes for encode/decode.

TX path: ftx_message_encode() → ft4_encode() → symbols_to_audio() → sounddevice + PTT
RX path: capture audio → build waterfall → ftx_find_candidates() → ftx_decode_candidate()

ft8_lib (kgoba/ft8_lib) must be installed as a shared library:
  Linux:   libft8.so
  macOS:   libft8.dylib
  Windows: ft8.dll

Without ft8_lib the codec is unavailable — both TX and RX are disabled.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

if TYPE_CHECKING:
    from comms.ft4.wsjt_decoder import Ft4WsjtDecoder

# ---------------------------------------------------------------------------
# FT4 physical-layer constants
# ---------------------------------------------------------------------------

SAMPLE_RATE: int = 12_000
FT4_SYMBOL_COUNT: int = 105
FT4_SAMPLES_PER_SYM: int = 576  # 12000 Hz / (500/24 baud) = exactly 576
FT4_SYMBOL_RATE: float = SAMPLE_RATE / FT4_SAMPLES_PER_SYM  # ≈ 20.833 Hz
FT4_TONE_SPACING: float = FT4_SYMBOL_RATE  # Hz between adjacent tones
FT4_TONE_COUNT: int = 4
FT4_PERIOD: float = 6.0  # period duration (s)
FT4_TX_OFFSET: float = 0.5  # TX starts 0.5 s into the period
FT4_TX_DURATION: float = FT4_SYMBOL_COUNT * FT4_SAMPLES_PER_SYM / SAMPLE_RATE  # ≈ 5.04 s

_PAYLOAD_BYTES: int = 10  # FTX_PAYLOAD_LENGTH_BYTES: 77 bits padded to 10 bytes
_MSG_BUFLEN: int = 25  # output buffer for ftx_message_decode
_MAX_CANDIDATES: int = 140
_LDPC_ITERATIONS: int = 25
_MIN_SYNC_SCORE: int = 10

# Waterfall frequency range for decode
_WF_F_MIN: float = 200.0
_WF_F_MAX: float = 3000.0

# Time/frequency oversampling ratios — matches kgoba/ft8_lib's demo decoder
# (kTime_osr / kFreq_osr in decode_ft8.c). Without oversampling,
# ftx_find_candidates() only synchronizes when the received symbol boundary
# happens to align exactly with our capture buffer, which is essentially
# never the case in practice (satellite Doppler, propagation delay, and the
# far station's own clock all shift the true symbol timing).
_WF_TIME_OSR: int = 2
_WF_FREQ_OSR: int = 2

# ftx_protocol_t enum values
_FTX_PROTOCOL_FT4: int = 0
_FTX_PROTOCOL_FT8: int = 1

# ---------------------------------------------------------------------------
# Decoded message dataclass
# ---------------------------------------------------------------------------


@dataclass
class Ft4Message:
    """One decoded FT4 message."""

    text: str
    freq_hz: float
    snr_db: float
    dt_sec: float  # time offset relative to period start


# ---------------------------------------------------------------------------
# ctypes structures (kgoba/ft8_lib current API)
# ---------------------------------------------------------------------------


class _FtxMessage(ctypes.Structure):
    """ftx_message_t: { uint8_t payload[10]; uint16_t hash; }"""

    _fields_ = [
        ("payload", ctypes.c_uint8 * _PAYLOAD_BYTES),
        ("hash", ctypes.c_uint16),
    ]


class _FtxWaterfall(ctypes.Structure):
    """ftx_waterfall_t: spectrogram passed to find/decode functions."""

    _fields_ = [
        ("max_blocks", ctypes.c_int),
        ("num_blocks", ctypes.c_int),
        ("num_bins", ctypes.c_int),
        ("time_osr", ctypes.c_int),
        ("freq_osr", ctypes.c_int),
        ("mag", ctypes.POINTER(ctypes.c_uint8)),
        ("block_stride", ctypes.c_int),
        ("protocol", ctypes.c_int),  # ftx_protocol_t enum
    ]


class _FtxCandidate(ctypes.Structure):
    """ftx_candidate_t: sync candidate returned by ftx_find_candidates."""

    _fields_ = [
        ("score", ctypes.c_int16),
        ("time_offset", ctypes.c_int16),
        ("freq_offset", ctypes.c_int16),
        ("time_sub", ctypes.c_uint8),
        ("freq_sub", ctypes.c_uint8),
    ]


class _FtxDecodeStatus(ctypes.Structure):
    """ftx_decode_status_t: decode result status."""

    _fields_ = [
        ("freq", ctypes.c_float),
        ("time", ctypes.c_float),
        ("ldpc_errors", ctypes.c_int),
        ("crc_extracted", ctypes.c_uint16),
        ("crc_calculated", ctypes.c_uint16),
    ]


# ---------------------------------------------------------------------------
# Library discovery
# ---------------------------------------------------------------------------


def get_user_ft8lib_dir() -> Path:
    """Return platform-specific user install directory for ft8lib."""
    from platformdirs import user_data_dir

    return Path(user_data_dir("fbsat59")) / "ft8lib"


def _find_ft8lib() -> ctypes.CDLL | None:
    """Try to load libft8 from user dir, system paths, and PyInstaller bundle."""
    user_dir = get_user_ft8lib_dir()
    candidates: list[str] = []

    if sys.platform == "win32":
        # Python 3.8+ no longer searches a loaded DLL's own directory for its
        # dependencies unless that directory is explicitly registered first.
        # ft8.dll (MinGW-built) depends on libgcc_s_seh-1.dll/libwinpthread-1.dll
        # sitting right next to it in user_dir; without this, ctypes.CDLL()
        # below fails with "module could not be found" even though ft8.dll
        # itself is present and readable (confirmed root cause of the same
        # class of failure for Hamlib — see main.py's os.add_dll_directory()
        # calls for _hamlib_user_dir).
        if user_dir.exists() and hasattr(os, "add_dll_directory"):
            with contextlib.suppress(OSError):
                os.add_dll_directory(str(user_dir))
        candidates.append(str(user_dir / "ft8.dll"))
        candidates.append("ft8.dll")
    elif sys.platform == "darwin":
        candidates.append(str(user_dir / "libft8.dylib"))
        candidates += ["libft8.dylib", "libft8.0.dylib"]
    else:
        candidates.append(str(user_dir / "libft8.so"))
        found = ctypes.util.find_library("ft8")
        if found:
            candidates.append(found)
        candidates += ["libft8.so", "libft8.so.0"]

    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", ""))
        for name in ("libft8.so", "ft8.dll", "libft8.dylib"):
            candidates.append(str(meipass / name))

    for path in candidates:
        try:
            lib = ctypes.CDLL(path)
            # Smoke test: essential encode symbols must exist
            _ = lib.ftx_message_encode
            _ = lib.ft4_encode
            return lib
        except (OSError, AttributeError):
            continue
    return None


def free_ft8lib(lib: ctypes.CDLL) -> None:
    """Release a library handle obtained from _find_ft8lib().

    ctypes never automatically unloads a shared library — on Windows the
    backing DLL file stays locked (can't be overwritten by a reinstall)
    for the lifetime of the process unless FreeLibrary is called
    explicitly. Only Windows needs this: POSIX filesystems allow replacing
    a file that a running process still has mapped.
    """
    if sys.platform == "win32":
        with contextlib.suppress(OSError, AttributeError):
            ctypes.windll.kernel32.FreeLibrary(lib._handle)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Bindings wrapper
# ---------------------------------------------------------------------------


class _Ft8LibBindings:
    """Thin ctypes wrapper around ft8_lib."""

    def __init__(self, lib: ctypes.CDLL) -> None:
        self._lib = lib
        self._decode_available: bool = False
        self._setup_encode_prototypes()
        self._setup_decode_prototypes()

    def _setup_encode_prototypes(self) -> None:
        # ftx_message_rc_t ftx_message_encode(ftx_message_t*, hash_if*, const char*)
        self._lib.ftx_message_encode.restype = ctypes.c_int
        self._lib.ftx_message_encode.argtypes = [
            ctypes.POINTER(_FtxMessage),
            ctypes.c_void_p,  # hash_if (NULL for standard messages)
            ctypes.c_char_p,
        ]
        # void ft4_encode(const uint8_t* payload, uint8_t* tones)
        self._lib.ft4_encode.restype = None
        self._lib.ft4_encode.argtypes = [
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.POINTER(ctypes.c_uint8),
        ]
        # ftx_message_rc_t ftx_message_decode(const ftx_message_t*, void*, char*, void*)
        self._lib.ftx_message_decode.restype = ctypes.c_int
        self._lib.ftx_message_decode.argtypes = [
            ctypes.POINTER(_FtxMessage),
            ctypes.c_void_p,  # hash_if (NULL)
            ctypes.c_char_p,
            ctypes.c_void_p,  # offsets (NULL)
        ]

    def _setup_decode_prototypes(self) -> None:
        try:
            # int ftx_find_candidates(const ftx_waterfall_t*, int, ftx_candidate_t[], int)
            self._lib.ftx_find_candidates.restype = ctypes.c_int
            self._lib.ftx_find_candidates.argtypes = [
                ctypes.POINTER(_FtxWaterfall),
                ctypes.c_int,
                ctypes.POINTER(_FtxCandidate),
                ctypes.c_int,
            ]
            # bool ftx_decode_candidate(const ftx_waterfall_t*, const ftx_candidate_t*,
            #                           int, ftx_message_t*, ftx_decode_status_t*)
            self._lib.ftx_decode_candidate.restype = ctypes.c_bool
            self._lib.ftx_decode_candidate.argtypes = [
                ctypes.POINTER(_FtxWaterfall),
                ctypes.POINTER(_FtxCandidate),
                ctypes.c_int,
                ctypes.POINTER(_FtxMessage),
                ctypes.POINTER(_FtxDecodeStatus),
            ]
            self._decode_available = True
        except AttributeError:
            pass  # older build without decode API

    # ------------------------------------------------------------------ #
    # Encode path                                                          #
    # ------------------------------------------------------------------ #

    def encode_message(self, text: str) -> bytes | None:
        """Encode text to 10-byte payload. Returns None on invalid message."""
        msg = _FtxMessage()
        rc = self._lib.ftx_message_encode(ctypes.byref(msg), None, text.encode("ascii"))
        return bytes(msg.payload) if rc == 0 else None

    def generate_tones(self, payload: bytes) -> bytes:
        """Generate 105 FT4 tone values (0-3) from 10-byte payload."""
        buf_in = (ctypes.c_uint8 * _PAYLOAD_BYTES)(*payload)
        buf_out = (ctypes.c_uint8 * FT4_SYMBOL_COUNT)()
        self._lib.ft4_encode(buf_in, buf_out)
        return bytes(buf_out)

    # ------------------------------------------------------------------ #
    # Decode path                                                          #
    # ------------------------------------------------------------------ #

    def decode_waterfall(
        self,
        mag_uint8: NDArray[np.uint8],
        num_blocks: int,
        num_bins: int,
        bin_hz: float,
    ) -> list[Ft4Message]:
        """Run ftx_lib sync + LDPC decode on a uint8 spectrogram.

        mag_uint8: shape (num_blocks, _WF_TIME_OSR * _WF_FREQ_OSR * num_bins),
        contiguous uint8 — as produced by compute_waterfall().
        bin_hz: Hz per (coarse) frequency bin.
        """
        if not self._decode_available:
            return []

        block_stride = _WF_TIME_OSR * _WF_FREQ_OSR * num_bins
        flat = np.ascontiguousarray(mag_uint8[:num_blocks, :block_stride], dtype=np.uint8)
        flat_p = flat.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8))

        wf = _FtxWaterfall(
            max_blocks=num_blocks,
            num_blocks=num_blocks,
            num_bins=num_bins,
            time_osr=_WF_TIME_OSR,
            freq_osr=_WF_FREQ_OSR,
            mag=flat_p,
            block_stride=block_stride,
            protocol=_FTX_PROTOCOL_FT4,
        )
        candidates = (_FtxCandidate * _MAX_CANDIDATES)()
        n_found = self._lib.ftx_find_candidates(
            ctypes.byref(wf), _MAX_CANDIDATES, candidates, _MIN_SYNC_SCORE
        )

        results: list[Ft4Message] = []
        msg_buf = ctypes.create_string_buffer(_MSG_BUFLEN)

        for i in range(n_found):
            msg = _FtxMessage()
            status = _FtxDecodeStatus()
            ok: bool = self._lib.ftx_decode_candidate(
                ctypes.byref(wf),
                ctypes.byref(candidates[i]),
                _LDPC_ITERATIONS,
                ctypes.byref(msg),
                ctypes.byref(status),
            )
            if ok:
                rc = self._lib.ftx_message_decode(ctypes.byref(msg), None, msg_buf, None)
                if rc == 0:
                    text = msg_buf.value.decode("ascii", errors="replace").strip()
                    min_bin = int(_WF_F_MIN / bin_hz)
                    freq = (
                        min_bin + candidates[i].freq_offset + candidates[i].freq_sub / _WF_FREQ_OSR
                    ) * bin_hz
                    # ft8_lib has no true SNR computation; its own demo decoder
                    # (demo/decode_ft8.c) uses this same score*0.5 approximation.
                    results.append(
                        Ft4Message(
                            text=text,
                            freq_hz=freq,
                            snr_db=candidates[i].score * 0.5,
                            dt_sec=float(status.time),
                        )
                    )
        return results


# ---------------------------------------------------------------------------
# Pure-Python audio helpers (no ft8_lib required)
# ---------------------------------------------------------------------------


def symbols_to_audio(
    tones: bytes,
    base_freq: float,
    sample_rate: int = SAMPLE_RATE,
) -> NDArray[np.float32]:
    """Generate phase-continuous 4-FSK audio from a FT4 tone array.

    Args:
        tones: Sequence of tone values (0-3), typically 105 bytes.
        base_freq: Frequency (Hz) of tone 0.
        sample_rate: Output audio sample rate.

    Returns:
        Float32 audio array of length len(tones) * samples_per_symbol.
    """
    spf = int(round(sample_rate / FT4_SYMBOL_RATE))
    total = len(tones) * spf
    audio = np.empty(total, dtype=np.float32)
    phase = 0.0
    pos = 0
    for tone in tones:
        freq = base_freq + tone * FT4_TONE_SPACING
        n = spf
        delta_phi = 2.0 * np.pi * freq / sample_rate
        phases = phase + delta_phi * np.arange(1, n + 1, dtype=np.float64)
        audio[pos : pos + n] = np.sin(phases).astype(np.float32)
        phase = float(phases[-1] % (2.0 * np.pi))
        pos += n
    return audio


def compute_waterfall(
    audio: NDArray[np.float32],
    sample_rate: int = SAMPLE_RATE,
) -> tuple[NDArray[np.uint8], int, int, float]:
    """Compute uint8 spectrogram for ft8_lib decode.

    Reproduces kgoba/ft8_lib's monitor_process() (common/monitor.c): a
    sliding analysis window of ``nfft = block_size * freq_osr`` samples,
    advanced in steps of ``subblock_size = block_size // time_osr``, windowed
    with the periodic Hann function ``sin(pi*i/nfft)^2`` and normalized by
    ``2/nfft``. Oversampling in time and frequency is what lets
    ftx_find_candidates() localize sync even when the transmitted symbol
    boundary does not line up exactly with our capture buffer.

    Returns (mag_uint8, num_blocks, num_bins, bin_hz) where mag_uint8 has
    shape (num_blocks, time_osr * freq_osr * num_bins) — one flattened
    [time_osr][freq_osr][num_bins] row per symbol, matching ftx_waterfall_t's
    block_stride layout.
    """
    block_size = FT4_SAMPLES_PER_SYM  # samples per symbol
    time_osr = _WF_TIME_OSR
    freq_osr = _WF_FREQ_OSR
    subblock_size = block_size // time_osr
    nfft = block_size * freq_osr

    bin_hz = sample_rate / block_size  # coarse (tone-spacing) bin width
    min_bin = int(_WF_F_MIN / bin_hz)
    max_bin = int(_WF_F_MAX / bin_hz) + 1
    num_bins = max_bin - min_bin

    num_blocks = len(audio) // block_size
    if num_blocks == 0:
        return np.zeros((0, num_bins), dtype=np.uint8), 0, num_bins, bin_hz

    i = np.arange(nfft, dtype=np.float64)
    window = (2.0 / nfft) * np.sin(np.pi * i / nfft) ** 2

    # last_frame in monitor_process() starts as (nfft - subblock_size) zeros
    # and slides forward by subblock_size samples per step, so at global step
    # s its content equals padded[s*subblock_size : s*subblock_size + nfft].
    padded = np.concatenate([np.zeros(nfft - subblock_size, dtype=np.float32), audio]).astype(
        np.float64
    )

    mag = np.zeros((num_blocks, time_osr, freq_osr, num_bins), dtype=np.uint8)
    bin_idx = np.arange(min_bin, max_bin)

    for s in range(num_blocks * time_osr):
        block = s // time_osr
        time_sub = s % time_osr
        start = s * subblock_size
        frame = padded[start : start + nfft] * window
        spec = np.fft.rfft(frame)
        for freq_sub in range(freq_osr):
            src = bin_idx * freq_osr + freq_sub
            mag2 = spec[src].real ** 2 + spec[src].imag ** 2
            db = 10.0 * np.log10(np.maximum(mag2, 1e-12))
            scaled = np.clip((2.0 * db + 240.0).astype(np.int64), 0, 255)
            mag[block, time_sub, freq_sub, :] = scaled.astype(np.uint8)

    flat = mag.reshape(num_blocks, time_osr * freq_osr * num_bins)
    return flat, num_blocks, num_bins, bin_hz


# ---------------------------------------------------------------------------
# Public codec class
# ---------------------------------------------------------------------------


class Ft4Codec:
    """High-level FT4 encode / decode interface.

    Audio synthesis (TX) and spectrogram computation are pure Python.
    LDPC encode/decode require ft8_lib shared library.
    """

    def __init__(self) -> None:
        _raw = _find_ft8lib()
        self._lib: _Ft8LibBindings | None = None
        if _raw is not None:
            with contextlib.suppress(AttributeError):
                self._lib = _Ft8LibBindings(_raw)
        self._wsjt_decoder: Ft4WsjtDecoder | None = None

    @property
    def is_available(self) -> bool:
        """True if ft8_lib is loaded and TX encode is functional."""
        return self._lib is not None

    @property
    def decode_available(self) -> bool:
        """True if ft8_lib's decode API is accessible."""
        return self._lib is not None and self._lib._decode_available

    @property
    def decode_backend(self) -> str:
        """Which RX decode engine decode_audio() will use.

        "wsjtx": full WSJT-X decode engine (libft4wsjt) — 3-pass subtract +
        BP/OSD, recommended. "ft8lib": lightweight single-pass fallback.
        "none": no RX decode available.
        """
        from comms.ft4.wsjt_decoder import is_available as _wsjt_available

        if _wsjt_available():
            return "wsjtx"
        if self.decode_available:
            return "ft8lib"
        return "none"

    def encode_audio(
        self,
        message: str,
        base_freq: float = 1000.0,
        sample_rate: int = SAMPLE_RATE,
    ) -> NDArray[np.float32] | None:
        """Encode a standard FT4 message to audio.

        Returns float32 audio array, or None if ft8_lib is unavailable
        or the message string is not valid FT4 format.
        """
        if self._lib is None:
            return None
        payload = self._lib.encode_message(message)
        if payload is None:
            return None
        tones = self._lib.generate_tones(payload)
        return symbols_to_audio(tones, base_freq, sample_rate)

    def decode_audio(
        self,
        audio: NDArray[np.float32],
        sample_rate: int = SAMPLE_RATE,
        my_call: str = "",
    ) -> list[Ft4Message]:
        """Decode FT4 messages from one period of recorded audio.

        Prefers the full WSJT-X decode engine (libft4wsjt, see
        wsjt_decoder.py) when installed, for much better recovery of
        weak/overlapping stations. Falls back to the lightweight ft8_lib
        single-pass decoder otherwise. Returns an empty list if neither is
        available.
        """
        from comms.ft4.wsjt_decoder import Ft4WsjtDecoder
        from comms.ft4.wsjt_decoder import is_available as _wsjt_available

        if _wsjt_available():
            if self._wsjt_decoder is None or self._wsjt_decoder.my_call != my_call.upper():
                self._wsjt_decoder = Ft4WsjtDecoder(my_call=my_call.upper())
            return self._wsjt_decoder.decode_audio(audio, sample_rate)

        if self._lib is None or not self._lib._decode_available:
            return []
        mag, num_blocks, num_bins, bin_hz = compute_waterfall(audio, sample_rate)
        if num_blocks == 0:
            return []
        return self._lib.decode_waterfall(mag, num_blocks, num_bins, bin_hz)
