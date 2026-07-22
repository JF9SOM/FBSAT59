"""Q65 codec — wraps libq65 (WSJT-X's own Q65 decode engine) via ctypes.

RX path: capture one T/R period of audio → q65wsjt_decode() (callback-based,
one invocation per decoded message) → decoded messages.

libq65 is built from wsjtx/wsjtx's lib/q65_decode.f90 and its dependency
closure (see scripts/build_q65lib.sh), wrapped through a small C ABI bridge
(scripts/wsjtx_bridge/q65wsjt_bridge.f90) — same approach as libft4wsjt
(src/comms/ft4/wsjt_decoder.py). It must be installed as a shared library:
  Linux:   libq65.so
  macOS:   libq65.dylib
  Windows: q65.dll

Install via Help > Q65 (libq65) Installation… or by running the
build-q65lib.yml workflow and downloading the result.

Without libq65 the codec is unavailable — decoding is disabled.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

# Longest Q65 period (300s @ 12000 Hz) — must match Q65WSJT_NMAX in
# scripts/wsjtx_bridge/q65wsjt_bridge.f90. Shorter buffers are zero-padded
# inside the library; longer ones truncated.
_Q65WSJT_NMAX: int = 300 * 12_000

_DecodeCallbackT = ctypes.CFUNCTYPE(
    None,
    ctypes.c_int,  # snr
    ctypes.c_float,  # dt
    ctypes.c_float,  # freq
    ctypes.c_char_p,  # decoded text (NUL-terminated)
    ctypes.c_int,  # idec (a priori decode type, 0 = none)
    ctypes.c_void_p,  # user_data (unused)
)

# ---------------------------------------------------------------------------
# Q65 physical-layer constants
# ---------------------------------------------------------------------------

SAMPLE_RATE: int = 12_000

# Period durations in seconds (A=60s is the EME standard on 144 MHz+)
Q65_PERIODS: dict[str, int] = {
    "Q65-60A": 60,
    "Q65-60B": 60,
    "Q65-30B": 30,
    "Q65-30C": 30,
    "Q65-15C": 15,
    "Q65-15D": 15,
    "Q65-15E": 15,
}

# Submode letter → index used by libq65
Q65_SUBMODE: dict[str, int] = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}


# ---------------------------------------------------------------------------
# Decoded message dataclass
# ---------------------------------------------------------------------------


@dataclass
class Q65Message:
    """One decoded Q65 message."""

    text: str
    freq_hz: float
    snr_db: float
    dt_sec: float


# ---------------------------------------------------------------------------
# libq65 loader
# ---------------------------------------------------------------------------

_USER_DIR_ENVVAR = "FBSAT59_Q65LIB_DIR"


def _find_libq65() -> Path | None:
    """Search for libq65 shared library in priority order.

    1. User-installed via Help > Q65 Installation
    2. Bundled inside PyInstaller _MEIPASS
    3. System path (development convenience)
    """
    import platformdirs

    candidates: list[Path] = []

    # 1. User-installed directory
    user_dir = Path(platformdirs.user_data_dir("fbsat59")) / "q65lib"
    if sys.platform == "win32":
        # Python 3.8+ no longer searches a loaded DLL's own directory for its
        # dependencies unless that directory is explicitly registered first.
        # q65.dll (MinGW/gfortran-built) depends on runtime DLLs (libgfortran,
        # libgcc_s_seh-1, libwinpthread-1, libquadmath, etc.) sitting right
        # next to it in user_dir; without this, ctypes.CDLL() below can fail
        # to resolve those dependencies even though q65.dll itself is present
        # and readable (same root cause fixed for Hamlib/ft8lib — see
        # main.py's os.add_dll_directory() calls and codec.py's
        # _find_ft8lib()).
        if user_dir.exists() and hasattr(os, "add_dll_directory"):
            with contextlib.suppress(OSError):
                os.add_dll_directory(str(user_dir))
        candidates.append(user_dir / "q65.dll")
    elif sys.platform == "darwin":
        candidates.append(user_dir / "libq65.dylib")
    else:
        candidates.append(user_dir / "libq65.so")

    # 2. PyInstaller bundle (_MEIPASS)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        mp = Path(meipass)
        for name in ("q65.dll", "libq65.dylib", "libq65.so"):
            candidates.append(mp / name)

    # 3. Development: repo-local q65lib-bundle/
    try:
        import importlib.util as _ilu

        spec = _ilu.find_spec("comms.q65.codec")
        if spec and spec.origin:
            repo_root = Path(spec.origin).parent.parent.parent.parent
            bundle = repo_root / "q65lib-bundle"
            for name in ("q65.dll", "libq65.dylib", "libq65.so"):
                candidates.append(bundle / name)
    except Exception:
        pass

    for p in candidates:
        if p.exists():
            return p
    return None


def _load_libq65() -> ctypes.CDLL | None:
    """Load libq65 and set up function signatures. Returns None if not found."""
    path = _find_libq65()
    if path is None:
        return None
    try:
        lib = ctypes.CDLL(str(path))
        # q65wsjt_decode(iwave, nsamples, ntrperiod, nsubmode, nfqso, nfa, nfb,
        #                ndepth, lclearave, emedelay, mycall, hiscall, hisgrid,
        #                callback, user_data)
        # Invokes callback once per decoded message; no return value.
        lib.q65wsjt_decode.restype = None
        lib.q65wsjt_decode.argtypes = [
            ctypes.POINTER(ctypes.c_int16),  # iwave
            ctypes.c_int,  # nsamples
            ctypes.c_int,  # ntrperiod (seconds)
            ctypes.c_int,  # nsubmode (0=A..4=E)
            ctypes.c_int,  # nfqso
            ctypes.c_int,  # nfa (Hz low)
            ctypes.c_int,  # nfb (Hz high)
            ctypes.c_int,  # ndepth
            ctypes.c_int,  # lclearave (nonzero clears cross-period averaging)
            ctypes.c_float,  # emedelay (seconds)
            ctypes.c_char_p,  # mycall
            ctypes.c_char_p,  # hiscall
            ctypes.c_char_p,  # hisgrid
            _DecodeCallbackT,  # callback
            ctypes.c_void_p,  # user_data
        ]
        return lib
    except OSError:
        return None


_lib: ctypes.CDLL | None = _load_libq65()


def is_available() -> bool:
    """Return True if libq65 is loaded and decoding is possible."""
    return _lib is not None


def lib_version() -> str:
    """Return libq65 version string, or empty string if unavailable.

    q65wsjt_decode's bridge does not export a version symbol; the bundled
    WSJT-X tag is recorded in version.txt next to the shared library
    (see scripts/build_q65lib.sh) instead.
    """
    if _lib is None:
        return ""
    with __import__("contextlib").suppress(Exception):
        path = _find_libq65()
        if path is not None:
            version_file = path.parent / "version.txt"
            if version_file.exists():
                return version_file.read_text().strip()
    return ""


# ---------------------------------------------------------------------------
# Q65Codec
# ---------------------------------------------------------------------------


class Q65Codec:
    """Q65 decoder backed by libq65 (WSJT-X's own decode engine).

    Args:
        submode: One of 'A', 'B', 'C', 'D', 'E'.  Default 'A' (EME standard).
        nfa: Low frequency bound for search (Hz).
        nfb: High frequency bound for search (Hz).
        nfqso: Partner frequency in Hz; 0 means search the full nfa–nfb range.
        my_call: Own callsign, used for a priori (AP) decoding of continuing
            QSOs. May be empty.
        hiscall, hisgrid: Partner callsign/grid, likewise used for AP
            decoding. May be empty.
        ndepth: Decode effort. Bits 0-1: 1=normal, 2=deep, 3=deepest
            (default). Bit 4 (16): enable cross-period averaging for weak
            EME signals — not set by default since this class always clears
            the accumulator per call (see clear_averaging below).
        emedelay: Extra sync-search delay (seconds) to cover EME path delay.
            Default 1.0s; pass 0.0 for non-EME point-to-point use.
    """

    def __init__(
        self,
        submode: str = "A",
        nfa: int = 200,
        nfb: int = 3000,
        nfqso: int = 0,
        my_call: str = "",
        hiscall: str = "",
        hisgrid: str = "",
        ndepth: int = 3,
        emedelay: float = 1.0,
    ) -> None:
        self.submode = submode.upper()
        self.nfa = nfa
        self.nfb = nfb
        self.nfqso = nfqso
        self.my_call = my_call
        self.hiscall = hiscall
        self.hisgrid = hisgrid
        self.ndepth = ndepth
        self.emedelay = emedelay

    def decode(self, samples: NDArray[np.float32], period_seconds: int = 60) -> list[Q65Message]:
        """Decode one complete Q65 audio period.

        Each call clears libq65's cross-period averaging accumulator first
        (lclearave), so results only reflect this one period — matching the
        original stateless-per-call behavior. Weak-EME cross-period
        averaging is a real WSJT-X capability but isn't wired up here yet.

        Args:
            samples: Float32 audio at SAMPLE_RATE Hz.
                     Length should be period_seconds * SAMPLE_RATE.
            period_seconds: T/R period length (15, 30, 60, 120, or 300).

        Returns:
            List of decoded Q65Message objects (empty if none decoded or
            library unavailable).
        """
        if _lib is None:
            return []

        n = min(len(samples), _Q65WSJT_NMAX)
        iwave = np.zeros(_Q65WSJT_NMAX, dtype=np.int16)
        iwave[:n] = np.clip(samples[:n], -1.0, 1.0) * 32767.0
        c_arr = iwave.ctypes.data_as(ctypes.POINTER(ctypes.c_int16))

        results: list[Q65Message] = []

        def _on_decode(
            snr: int,
            dt: float,
            freq: float,
            decoded: bytes | None,
            idec: int,
            user_data: int | None,
        ) -> None:
            del idec, user_data  # unused
            if decoded is None:
                return
            text = ctypes.string_at(decoded).decode("ascii", errors="replace").strip()
            if text:
                results.append(
                    Q65Message(text=text, freq_hz=float(freq), snr_db=float(snr), dt_sec=float(dt))
                )

        cb = _DecodeCallbackT(_on_decode)
        try:
            _lib.q65wsjt_decode(
                c_arr,
                ctypes.c_int(len(iwave)),
                ctypes.c_int(period_seconds),
                ctypes.c_int(Q65_SUBMODE.get(self.submode, 0)),
                ctypes.c_int(self.nfqso),
                ctypes.c_int(self.nfa),
                ctypes.c_int(self.nfb),
                ctypes.c_int(self.ndepth),
                ctypes.c_int(1),  # lclearave: always clear (stateless per call)
                ctypes.c_float(self.emedelay),
                self.my_call.upper().encode("ascii", errors="ignore"),
                self.hiscall.upper().encode("ascii", errors="ignore"),
                self.hisgrid.upper().encode("ascii", errors="ignore"),
                cb,
                None,
            )
        except Exception:
            return []
        return results
