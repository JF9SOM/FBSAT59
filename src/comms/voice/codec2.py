"""CODEC2 700C decoder through libcodec2 (ctypes).

libcodec2 (LGPL-2.1, https://github.com/drowe67/codec2) is loaded at run time, never linked
into the app. It is looked for, in order, in the user data folder (``codec2/``), next to a
frozen app, in the gr-satellites install the Telemetry tab uses (its conda environment ships
libcodec2), then on the system (``find_library`` and the usual Homebrew / distro folders).
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import sys
from collections.abc import Sequence
from pathlib import Path

import numpy as np

MODE_700C = 8  # CODEC2_MODE_700C in codec2.h
SAMPLE_RATE = 8000
_NAMES = {
    "win32": ("libcodec2.dll", "codec2.dll"),
    "darwin": ("libcodec2.dylib", "libcodec2.1.2.dylib"),
}
_LINUX_NAMES = ("libcodec2.so", "libcodec2.so.1.2", "libcodec2.so.1.1", "libcodec2.so.1.0")


def _library_names() -> tuple[str, ...]:
    return _NAMES.get(sys.platform, _LINUX_NAMES)


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    try:
        from platformdirs import user_data_dir

        dirs.append(Path(user_data_dir("fbsat59")) / "codec2")
    except ImportError:
        pass
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass))
    try:
        from comms.telemetry.gr_satellites_install import user_gr_satellites_dir

        env = user_gr_satellites_dir()
        dirs += [env / "lib", env / "Library" / "bin", env / "bin"]
    except Exception:  # pragma: no cover - the install helper is optional
        pass
    dirs += [
        Path("/opt/homebrew/lib"),
        Path("/usr/local/lib"),
        Path("/usr/lib"),
        Path("/usr/lib/x86_64-linux-gnu"),
        Path("/usr/lib/aarch64-linux-gnu"),
    ]
    return dirs


def find_codec2_library() -> str | None:
    """Path (or name) of a loadable libcodec2, or None."""
    for folder in _candidate_dirs():
        for name in _library_names():
            path = folder / name
            if path.is_file():
                return str(path)
    return ctypes.util.find_library("codec2")


def is_available() -> bool:
    """True if libcodec2 can be found and loaded."""
    try:
        _load()
    except OSError:
        return False
    return True


_lib: ctypes.CDLL | None = None


def _load() -> ctypes.CDLL:
    global _lib
    if _lib is not None:
        return _lib
    path = find_codec2_library()
    if path is None:
        raise OSError("libcodec2 not found")
    lib = ctypes.CDLL(path)
    lib.codec2_create.restype = ctypes.c_void_p
    lib.codec2_create.argtypes = [ctypes.c_int]
    lib.codec2_destroy.restype = None
    lib.codec2_destroy.argtypes = [ctypes.c_void_p]
    lib.codec2_samples_per_frame.restype = ctypes.c_int
    lib.codec2_samples_per_frame.argtypes = [ctypes.c_void_p]
    lib.codec2_bytes_per_frame.restype = ctypes.c_int
    lib.codec2_bytes_per_frame.argtypes = [ctypes.c_void_p]
    lib.codec2_decode.restype = None
    lib.codec2_decode.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p]
    _lib = lib
    return lib


class Codec2Decoder:
    """One CODEC2 700C decoder (it keeps state between frames: use one per voice run)."""

    def __init__(self, mode: int = MODE_700C) -> None:
        self._lib = _load()
        self._state = self._lib.codec2_create(mode)
        if not self._state:
            raise OSError("codec2_create failed")
        self.samples_per_frame: int = int(self._lib.codec2_samples_per_frame(self._state))
        self.bytes_per_frame: int = int(self._lib.codec2_bytes_per_frame(self._state))

    def decode(self, frame: bytes) -> np.ndarray:
        """Decode one frame (``bytes_per_frame`` bytes, bits MSB first) to int16 samples."""
        if len(frame) != self.bytes_per_frame:
            raise ValueError(f"frame must be {self.bytes_per_frame} bytes")
        out = np.zeros(self.samples_per_frame, dtype=np.int16)
        self._lib.codec2_decode(self._state, out.ctypes.data, frame)
        return out

    def close(self) -> None:
        """Free the decoder."""
        if self._state:
            self._lib.codec2_destroy(self._state)
            self._state = None

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown order
        with contextlib.suppress(Exception):
            self.close()


def decode_stream(frames: Sequence[bytes], mode: int = MODE_700C) -> np.ndarray:
    """Decode consecutive frames (in the library's byte-per-frame form) to int16 samples."""
    dec = Codec2Decoder(mode)
    try:
        if not frames:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate([dec.decode(f) for f in frames])
    finally:
        dec.close()
