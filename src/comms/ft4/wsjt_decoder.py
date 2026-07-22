"""Ft4WsjtDecoder — wraps libft4wsjt (built from WSJT-X's own FT4 decode
engine) via ctypes.

kgoba/ft8_lib (see codec.py) is a lightweight single-pass reference
decoder; it lacks WSJT-X's 3-pass signal subtraction and BP/OSD hybrid
decode, so it misses many overlapping or weak stations in a crowded
FT4 period. libft4wsjt exposes the actual WSJT-X decode engine
(lib/ft4_decode.f90 in wsjtx/wsjtx) through a small C ABI bridge
(scripts/wsjtx_bridge/ft4wsjt_bridge.f90), built by
scripts/build_ft4wsjt.sh.

libft4wsjt must be installed as a shared library:
  Linux:   libft4wsjt.so
  macOS:   libft4wsjt.dylib
  Windows: ft4wsjt.dll

Install via Help > FT4 Enhanced Decoder Installation… or by running
build_ft4wsjt.sh / the build-ft4wsjt.yml workflow and downloading the
result.

Without libft4wsjt, is_available() is False and Ft4Codec.decode_audio()
(codec.py) falls back to the ft8_lib single-pass decoder. TX encoding
always uses ft8_lib regardless of this module's availability.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import sys
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from comms.ft4.codec import SAMPLE_RATE, Ft4Message

# Fixed input length expected by libft4wsjt (21*3456 samples @ 12000 Hz =
# 6.048s), matching lib/ft4/ft4_params.f90's NMAX in wsjtx/wsjtx. Shorter or
# longer buffers are zero-padded/truncated inside the library.
FT4WSJT_NMAX: int = 21 * 3456

_CallbackT = ctypes.CFUNCTYPE(
    None,
    ctypes.c_float,  # sync
    ctypes.c_int,  # snr
    ctypes.c_float,  # dt
    ctypes.c_float,  # freq
    ctypes.c_char_p,  # decoded text (NUL-terminated)
    ctypes.c_int,  # nap (a priori decode type, 0 = none)
    ctypes.c_float,  # qual
    ctypes.c_void_p,  # user_data (unused)
)


def get_user_ft4wsjt_dir() -> Path:
    """Return platform-specific user install directory for libft4wsjt."""
    from platformdirs import user_data_dir

    return Path(user_data_dir("fbsat59")) / "ft4wsjt"


def _find_libft4wsjt() -> Path | None:
    """Search for libft4wsjt in priority order.

    1. User-installed via Help > FT4 Enhanced Decoder Installation
    2. Bundled inside PyInstaller _MEIPASS
    3. System path (development convenience)
    """
    candidates: list[Path] = []

    user_dir = get_user_ft4wsjt_dir()
    if sys.platform == "win32":
        # Python 3.8+ no longer searches a loaded DLL's own directory for its
        # dependencies unless that directory is explicitly registered first.
        # ft4wsjt.dll (gfortran/MinGW-built, FFTW3 + Boost) depends on
        # runtime/FFTW3 DLLs sitting right next to it in user_dir; without
        # this, ctypes.CDLL() below can fail to resolve those dependencies
        # even though ft4wsjt.dll itself is present and readable (same root
        # cause fixed for Hamlib/ft8lib/libq65 — see main.py's
        # os.add_dll_directory() calls and codec.py's _find_ft8lib()).
        if user_dir.exists() and hasattr(os, "add_dll_directory"):
            with contextlib.suppress(OSError):
                os.add_dll_directory(str(user_dir))
        candidates.append(user_dir / "ft4wsjt.dll")
    elif sys.platform == "darwin":
        candidates.append(user_dir / "libft4wsjt.dylib")
    else:
        candidates.append(user_dir / "libft4wsjt.so")

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        mp = Path(meipass)
        for name in ("ft4wsjt.dll", "libft4wsjt.dylib", "libft4wsjt.so"):
            candidates.append(mp / name)

    with contextlib.suppress(Exception):
        import importlib.util as _ilu

        spec = _ilu.find_spec("comms.ft4.wsjt_decoder")
        if spec and spec.origin:
            repo_root = Path(spec.origin).parent.parent.parent.parent
            bundle = repo_root / "ft4wsjt-bundle"
            for name in ("ft4wsjt.dll", "libft4wsjt.dylib", "libft4wsjt.so"):
                candidates.append(bundle / name)

    for p in candidates:
        if p.exists():
            return p
    return None


def _load_libft4wsjt() -> ctypes.CDLL | None:
    """Load libft4wsjt and set up function signatures. None if not found."""
    path = _find_libft4wsjt()
    if path is None:
        return None
    try:
        lib = ctypes.CDLL(str(path))
        lib.ft4wsjt_decode.restype = None
        lib.ft4wsjt_decode.argtypes = [
            ctypes.POINTER(ctypes.c_int16),  # iwave
            ctypes.c_int,  # nsamples
            ctypes.c_int,  # nfqso
            ctypes.c_int,  # nfa
            ctypes.c_int,  # nfb
            ctypes.c_int,  # ndepth
            ctypes.c_char_p,  # mycall
            ctypes.c_char_p,  # hiscall
            _CallbackT,  # callback
            ctypes.c_void_p,  # user_data
        ]
        with contextlib.suppress(AttributeError):
            lib.ft4wsjt_expected_samples.restype = ctypes.c_int
        return lib
    except OSError:
        return None


_lib: ctypes.CDLL | None = _load_libft4wsjt()


def is_available() -> bool:
    """Return True if libft4wsjt is loaded and full-depth decoding is possible."""
    return _lib is not None


class Ft4WsjtDecoder:
    """FT4 RX decoder backed by WSJT-X's own 3-pass subtract + BP/OSD engine.

    Args:
        my_call: Own callsign, used for a priori (AP) decoding of continuing
            QSOs. May be empty.
        ndepth: 1 = single BP pass (no subtraction), 2 = 3-pass
            subtract+BP only, 3 = 3-pass subtract+BP+OSD (full WSJT-X
            depth; default).
    """

    def __init__(self, my_call: str = "", ndepth: int = 3) -> None:
        self.my_call = my_call
        self.ndepth = ndepth

    def decode_audio(
        self,
        audio: NDArray[np.float32],
        sample_rate: int = SAMPLE_RATE,
        nfa: int = 200,
        nfb: int = 3000,
        nfqso: int = 1000,
    ) -> list[Ft4Message]:
        """Decode one FT4 period (~6s) of audio.

        Args:
            audio: Float32 samples at sample_rate Hz, ideally spanning one
                6.048s period (FT4WSJT_NMAX samples).
            sample_rate: Must equal SAMPLE_RATE; resampling is the caller's
                responsibility.
            nfa, nfb: Search band (Hz).
            nfqso: Partner/dial frequency (Hz), used for AP frequency hints.

        Returns:
            Decoded messages, or an empty list if the library is
            unavailable or decoding raises.
        """
        if _lib is None or sample_rate != SAMPLE_RATE:
            return []

        iwave = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
        c_arr = iwave.ctypes.data_as(ctypes.POINTER(ctypes.c_int16))

        results: list[Ft4Message] = []

        def _on_decode(
            sync: float,
            snr: int,
            dt: float,
            freq: float,
            decoded: bytes | None,
            nap: int,
            qual: float,
            user_data: int | None,
        ) -> None:
            del sync, nap, qual, user_data  # unused
            if decoded is None:
                return
            text = ctypes.string_at(decoded).decode("ascii", errors="replace").strip()
            if text:
                results.append(
                    Ft4Message(text=text, freq_hz=float(freq), snr_db=float(snr), dt_sec=float(dt))
                )

        cb = _CallbackT(_on_decode)
        try:
            _lib.ft4wsjt_decode(
                c_arr,
                ctypes.c_int(len(iwave)),
                ctypes.c_int(nfqso),
                ctypes.c_int(nfa),
                ctypes.c_int(nfb),
                ctypes.c_int(self.ndepth),
                self.my_call.upper().encode("ascii", errors="ignore"),
                b"",
                cb,
                None,
            )
        except Exception:
            return []
        return results
