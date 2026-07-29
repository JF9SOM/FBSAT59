"""
Hamlib transceiver and rotator control module

RigController          — Abstract base class for transceiver control
HamlibDirectController — Direct serial port connection via python-hamlib
HamlibNetController    — TCP connection to rigctld (compatible with GPredict NET Control)
RotatorController      — Abstract base class for rotator control
HamlibRotatorController — Hamlib rotator control
HamlibVersionChecker   — Check the installed Hamlib version

Automatically falls back to a mock when Hamlib is not installed,
so tests pass even in CI environments without python-hamlib.
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import os
import socket
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sdr.device import SdrDevice, SdrDeviceInfo
    from sdr.pipeline import SDRPipeline

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ensure only Hamlib 4.7.1 is loaded — loading 4.5.5 and 4.7.1 simultaneously
# causes a "Hash collision" fatal error in Hamlib's internal rig registry.
# Remove the system dist-packages entry so Python cannot find the old _Hamlib.so,
# then prepend the 4.7.1 path. LD_LIBRARY_PATH is not touched; _Hamlib.so's
# RUNPATH already resolves libhamlib.so to /opt/hamlib/4.7/lib.
# ---------------------------------------------------------------------------
_HAMLIB_471_PY = "/opt/hamlib/4.7/lib/python3.12/site-packages"
_HAMLIB_SYS_PY = "/usr/lib/python3/dist-packages"
if _HAMLIB_SYS_PY in sys.path:
    sys.path.remove(_HAMLIB_SYS_PY)
if os.path.exists(_HAMLIB_471_PY) and _HAMLIB_471_PY not in sys.path:
    sys.path.insert(0, _HAMLIB_471_PY)

# ---------------------------------------------------------------------------
# Hamlib availability check — import is deferred to connect() to avoid loading
# the shared library at startup, which collides with Qt's thread-local storage.
# ---------------------------------------------------------------------------

HAMLIB_AVAILABLE: bool = importlib.util.find_spec("Hamlib") is not None
if not HAMLIB_AVAILABLE:
    logger.warning(
        "python-hamlib not found — running in mock mode. "
        "Install libhamlib-dev and python3-hamlib to enable real rig control."
    )


# ---------------------------------------------------------------------------
# Mode mapping (SATNOGS mode string → Hamlib constant)
# ---------------------------------------------------------------------------


def _build_mode_map() -> dict[str, int]:
    """SATNOGS mode string → Hamlib RIG_MODE_* integer constant.

    Values are the stable public Hamlib bitmask constants (unchanged across
    versions), so no Hamlib import is needed at module load time.
    USB appears before SSB so SSB wins in the reverse map (last-wins dict
    comprehension), matching the canonical SATNOGS name.
    """
    return {
        "DIGITALVOICE": 32,  # RIG_MODE_FM
        "USB": 4,  # RIG_MODE_USB  (alias; SSB wins in reverse map)
        "FM": 32,  # RIG_MODE_FM
        "SSB": 4,  # RIG_MODE_USB  (canonical SATNOGS name; wins in reverse map)
        "LSB": 8,  # RIG_MODE_LSB
        "CW": 2,  # RIG_MODE_CW
        "CW-R": 128,  # RIG_MODE_CWR
        "BPSK": 2048,  # RIG_MODE_PKTUSB
        "AFSK": 4096,  # RIG_MODE_PKTFM
        "AM": 1,  # RIG_MODE_AM
        "USB-D": 2048,  # RIG_MODE_PKTUSB (data mode, e.g. FT4 calling freqs)
        "LSB-D": 1024,  # RIG_MODE_PKTLSB
    }


MODE_MAP: dict[str, int] = _build_mode_map()


def _build_live_hamlib_mode_map(_H: Any) -> dict[str, int]:
    """SATNOGS mode string → live Hamlib RIG_MODE_* constant.

    Unlike MODE_MAP (built from stable public bitmask values so it can exist
    at module load time), this reads the constants off an imported Hamlib
    module. Shared by every call site that opens a fresh Hamlib session to
    set mode (send_mode_only, _apply_mode_and_ctcss_hamlib,
    _resend_mode_ctcss_via_rig) so a mode added in one path can't silently
    stay missing in another — the exact gap that let FT4 fall back to FM.
    """
    return {
        "FM": _H.RIG_MODE_FM,
        "DIGITALVOICE": _H.RIG_MODE_FM,
        # AFSK (e.g. APRS) is carried over FM; PKTFM is not universally
        # supported (IC-9100 ignores it and leaves the rig in the previous
        # mode).  Plain FM is the correct receiver mode for APRS monitoring.
        "AFSK": _H.RIG_MODE_FM,
        "USB": _H.RIG_MODE_USB,
        "SSB": _H.RIG_MODE_USB,
        "LSB": _H.RIG_MODE_LSB,
        "CW": _H.RIG_MODE_CW,
        "CW-R": _H.RIG_MODE_CWR,
        "AM": _H.RIG_MODE_AM,
        "BPSK": _H.RIG_MODE_PKTUSB,
        "USB-D": _H.RIG_MODE_PKTUSB,  # data mode, e.g. FT4 calling freqs
        "LSB-D": _H.RIG_MODE_PKTLSB,
    }


# Preset CAT command templates for known rigs that need custom CTCSS commands.
# Keyed by ctcss_method value; value is (cat_on_template, cat_off_template).
# {tone:03d} is replaced at send time with the 3-digit CTCSS_TABLE index.
# Defined here so both the dialog (rig_dialog.py) and the loader
# (_load_rig_settings in main_window.py) always use the same authoritative values,
# avoiding stale DB entries after a preset correction.
CTCSS_PRESET_TEMPLATES: dict[str, tuple[str, str]] = {
    # FTX-1: CN P1 P2 P3P3P3; — P1=1 (Sub), P2=0 (CTCSS), P3=tone index 000-049
    "ftx1": ("CN10{tone:03d};CT11;", "CT10;"),
    # FT-991/FT-991A: CN P1 P2 P3P3P3; — P1=0 (fixed), P2=0 (CTCSS), P3=tone index 000-049
    # CT P1 P2; — P1=0 (fixed), P2=2 (CTCSS ENC only); CT00; to disable
    "ft991": ("CN00{tone:03d};CT02;", "CT00;"),
}

# CTCSS tone frequency (Hz) → rig index used in custom CAT commands.
# Covers the standard 50-tone table; gaps are intentional (some tone numbers
# are omitted from the FTX-1F documentation).
CTCSS_TABLE: dict[float, int] = {
    67.0: 0,
    69.3: 1,
    71.9: 2,
    74.4: 3,
    77.0: 4,
    79.7: 5,
    82.5: 6,
    85.4: 7,
    88.5: 8,
    91.5: 9,
    94.8: 10,
    97.4: 11,
    100.0: 12,
    103.5: 13,
    107.2: 14,
    110.9: 15,
    114.8: 16,
    118.8: 17,
    123.0: 18,
    127.3: 19,
    131.8: 20,
    136.5: 21,
    141.3: 22,
    146.2: 23,
    151.4: 24,
    156.7: 25,
    159.8: 26,
    162.2: 27,
    165.5: 28,
    167.9: 29,
    171.3: 30,
    173.8: 31,
    177.3: 32,
    183.5: 34,
    186.2: 35,
    189.9: 36,
    192.8: 37,
    196.6: 38,
    199.5: 39,
    203.5: 40,
    206.5: 41,
    210.7: 42,
    218.1: 43,
    225.7: 44,
    229.1: 45,
    233.6: 46,
    241.8: 47,
    250.3: 48,
    254.1: 49,
}


# ---------------------------------------------------------------------------
# Icom satmode rig identifiers
# ---------------------------------------------------------------------------
# Direct mode: model IDs used by HamlibDirectController._satmode
_SATMODE_RIG_IDS: frozenset[int] = frozenset(
    [
        3081,  # IC-9700  (rigctl -l verified 2026-06-15)
        3068,  # IC-9100  (rigctl -l verified 2026-06-15)
        3044,  # IC-910   (rigctl -l verified 2026-06-15)
        3034,  # IC-821H  (rigctl -l verified 2026-06-15)
    ]
)

# IC-9700 requires RIG_VFO_SUB for UL writes in satmode.
# IC-9100/IC-910H/IC-821H require RIG_VFO_TX (confirmed 2026-06-21).
# Background: IC-9700's Hamlib backend does not correctly route RIG_VFO_TX
# to Sub in satmode; RIG_VFO_SUB addresses Sub directly and works reliably.
# IC-9100 exhibits the opposite behaviour: RIG_VFO_SUB fails on the 2nd
# consecutive call, while RIG_VFO_TX works across multiple calls.
_SATMODE_USE_VFO_SUB: frozenset[int] = frozenset(
    [
        3081,  # IC-9700
    ]
)


def normalize_civ_addr(text: str) -> str:
    """Normalise a user-entered CI-V address into a Hamlib-parseable "0xNN" string.

    Accepts plain hex ("A2"), "0x"-prefixed hex ("0xA2"), and the trailing
    "h"/"H" hex suffix shown on Icom rig CI-V Address menus (e.g. "A2h") --
    users tend to copy what the rig itself displays rather than prepend
    "0x". Without stripping the trailing h, "A2h" is not a valid input to
    Hamlib's strtol()-based config parser or Python's int(x, 16), so the
    address would silently fail to apply (or, worse, silently fall back to
    the wrong default).
    """
    addr = text.strip()
    if addr.lower().endswith("h"):
        addr = addr[:-1].strip()
    if addr and not addr.lower().startswith("0x"):
        addr = "0x" + addr
    return addr


# NET mode: rigctld reports the connected rig name via the _ command.

# ---------------------------------------------------------------------------
# FTX-1F model identifiers (Direct mode raw CAT path)
# ---------------------------------------------------------------------------
_FTX1_MODEL_IDS: frozenset[int] = frozenset({1051})  # FTX-1F (Hamlib model 1051)

# FT-991 / FT-991A / FT-991AM (Hamlib model 1035) Direct mode raw CAT path.
_FT991_DIRECT_MODEL_IDS: frozenset[int] = frozenset({1035})

# IC-705 (Hamlib model 3085): Hamlib's set_split_vfo() intermittently rejects
# the call with "unsupported split" for this backend (confirmed live —
# same class of SWIG/binding flakiness as send_raw() and get_func()
# readbacks elsewhere in this file), so split ON/OFF is sent as a raw CI-V
# frame instead (C_CTL_SPLT=0x0F, S_SPLT_ON=0x01/S_SPLT_OFF=0x00), the same
# approach already used for FTX-1F/FT-991 above.
_IC705_MODEL_IDS: frozenset[int] = frozenset({3085})
_IC705_DEFAULT_CIV_ADDR = 0xA4

# FTX-1F CAT mode codes: MD P1 P2; where P1=0=MAIN, P1=1=SUB
_FTX1_MODE_CODES: dict[str, str] = {
    "FM": "4",
    "DIGITALVOICE": "4",
    "AFSK": "4",
    "USB": "2",
    "SSB": "2",
    "BPSK": "2",
    "LSB": "1",
    "CW": "3",  # CW-U
    "CW-R": "7",  # CW-L
    "AM": "5",
    "USB-D": "C",  # DATA-USB (data mode, e.g. FT4 calling freqs)
    "LSB-D": "8",  # DATA-LSB
}

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


class RigState(Enum):
    """Transceiver connection state."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class RigInfo:
    """Information about the connected transceiver."""

    model_id: int
    model_name: str
    port: str
    baud_rate: int
    state: RigState = RigState.DISCONNECTED


@dataclass
class FrequencyState:
    """Current frequency and mode state."""

    freq_hz: float = 0.0
    mode: str = "FM"
    passband_hz: int = 0
    ctcss_tone: float = 0.0  # Hz (0.0 = off)
    dcs_code: int = 0  # 0 = off


@dataclass
class RotatorState:
    """Rotator state."""

    azimuth_deg: float = 0.0
    elevation_deg: float = 0.0
    is_moving: bool = False


@dataclass
class VersionInfo:
    """Hamlib version information and update check result."""

    installed: str
    latest: str
    is_outdated: bool
    release_url: str = ""
    warning_message: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if self.is_outdated:
            self.warning_message = (
                f"Hamlib {self.installed} is installed, "
                f"but {self.latest} is available. "
                f"Consider upgrading: {self.release_url}"
            )


class RigControlError(Exception):
    """Transceiver control error (raised on rigctld command failure or communication error)."""


def _check_rig_ok(rig: Any, what: str) -> None:
    """Raise RigControlError if the rig's last Hamlib call failed.

    IMPORTANT: Hamlib's Python (SWIG) binding does NOT return the C API's
    int status code from Rig methods -- open()/close()/set_freq()/
    set_mode()/set_func()/set_split_vfo() etc. all return None regardless
    of outcome (confirmed empirically against the bundled 4.7.1 build: a
    deliberately-failing open() that the C layer reports as
    "rig_open returning2(-2)" still yields `None` in Python). The actual
    result is written to the rig's `error_status` attribute (the Python
    exposure of the C API's per-Rig last-error field) and must be read
    from there immediately after each call instead of from the call's own
    return value.

    Call immediately after the Hamlib call whose outcome is being checked
    (error_status reflects only the most recent operation).
    """
    status = rig.error_status
    if status != 0:  # RIG_OK == 0
        raise RigControlError(f"{what} failed (Hamlib error {status})")


_hamlib_trace_lock = threading.Lock()
_hamlib_file_trace_enabled = False


def _hamlib_trace_log_path() -> str | None:
    """Path to a TEMPORARY Hamlib CI-V trace log, next to fbsat59.log.

    Diagnostic for the still-unsolved Windows IC-9100 issue: rig.open()
    times out (Hamlib error -5) on every attempt even with a
    friend-verified-correct, matching-on-both-ends CI-V address (2026-07-20
    controlled test: same failure with A2/A2 and with A3/A3), and even
    with _open_rig_with_retry()'s 3 attempts -- so this is not the
    transient Windows COM-port timing glitch that function's docstring
    was written for; the real cause is still unknown.

    Remove this function, _hamlib_trace_lock, _hamlib_file_trace_enabled,
    and _hamlib_ensure_file_trace() once root-caused.
    """
    try:
        from platformdirs import user_log_dir

        log_dir = user_log_dir("fbsat59", "fbsat59")
        os.makedirs(log_dir, exist_ok=True)
        return os.path.join(log_dir, "hamlib_trace.log")
    except Exception:
        return None


def _find_hamlib_library_path() -> str | None:
    """Locate the actual bundled Hamlib shared library file on disk.

    Used only by _hamlib_ensure_file_trace() to get a full path for
    ctypes.CDLL(), instead of guessing a bare filename and relying on
    the OS's DLL/shared-library search path to resolve it -- confirmed
    live (2026-07-21) that neither "libhamlib-4.dll" nor "libhamlib.dll"
    matched this project's actual Windows bundle. fbsat59.spec collects
    every *.dll matched from the official Hamlib Windows release zip
    (the CI build script itself only pattern-matches "*hamlib*.dll"
    rather than hardcoding an exact name) into the PyInstaller bundle
    root, which for this project's onedir build may be nested under
    _internal/ next to the .exe (PyInstaller 6+ default) rather than
    directly beside it.

    Searches near wherever the already-imported Hamlib Python module
    itself was loaded from first (most reliable: DLL dependencies are
    normally co-located with the extension module that needs them, and
    main.py's os.add_dll_directory() calls assume exactly this), then
    falls back to a recursive search from the running executable's own
    directory.
    """
    import glob

    search_roots: list[str] = []
    with contextlib.suppress(Exception):
        import Hamlib as _H

        mod_file = getattr(_H, "__file__", None)
        if mod_file:
            search_roots.append(os.path.dirname(os.path.abspath(mod_file)))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        search_roots.append(meipass)
    with contextlib.suppress(Exception):
        search_roots.append(os.path.dirname(os.path.abspath(sys.executable)))

    seen: set[str] = set()
    patterns = ("*hamlib*.dll", "*hamlib*.dylib", "libhamlib*.so*")
    for root in search_roots:
        if not root or root in seen or not os.path.isdir(root):
            continue
        seen.add(root)
        for pattern in patterns:
            for path in glob.glob(os.path.join(root, pattern)):
                return path
            for path in glob.glob(os.path.join(root, "**", pattern), recursive=True):
                return path
    return None


def _hamlib_ensure_file_trace() -> None:
    """Idempotently redirect Hamlib's own debug stream to
    hamlib_trace.log for the rest of the process's lifetime.

    First attempt (os.dup2() on the process's real stderr fd, around each
    open() call) confirmed live (2026-07-21, real IC-9100 on the
    developer's own Windows 11 PC) to create the file but capture zero
    actual Hamlib output -- only the empty header lines this code used to
    write. Root cause: the bundled Hamlib is built with MinGW GCC (see
    CLAUDE.md's Hamlib bundling notes) while the frozen app's Python
    interpreter uses MSVC's C runtime; on Windows each CRT keeps its own
    separate stdio/fd table, so Python's os.dup2() on "fd 2" only affects
    the MSVC CRT's own table and never reaches whatever the MinGW-built
    Hamlib DLL's C library considers its own `stderr` FILE*.

    Sidesteps this entirely via rig_set_debug_filename(const char*), a
    real exported Hamlib C API function (src/debug.c, HAMLIB_API) that
    Hamlib itself resolves with its own fopen() -- so the FILE* it uses
    is guaranteed to belong to the same CRT as the rig_debug() calls that
    write to it, no cross-CRT fd trickery needed. Not exposed by this
    build's SWIG Python binding (only rig_set_debug(level),
    rig_get_debug(), rig_set_debug_time_stamp(), add2debugmsgsave() are --
    confirmed by introspecting the actual bundled module), so called here
    via ctypes directly against the already-loaded Hamlib library instead.

    rig_debug_stream is a single process-wide global in Hamlib's C code
    (shared by every Rig object/thread), and rig_set_debug_filename()
    opens its target in "w" (truncate) mode -- so this must be called at
    most once per process, not once per open() attempt, or later calls
    would each wipe out the previous attempts' trace.
    """
    global _hamlib_file_trace_enabled
    if _hamlib_file_trace_enabled:
        return
    with _hamlib_trace_lock:
        if _hamlib_file_trace_enabled:  # re-check inside the lock
            return
        trace_path = _hamlib_trace_log_path()
        if trace_path is None:
            return
        try:
            import ctypes

            with contextlib.suppress(Exception):
                import Hamlib as _H

                _H.rig_set_debug(_H.RIG_DEBUG_TRACE)

            # Bare guessed names ("libhamlib-4.dll", "libhamlib.dll")
            # confirmed live (2026-07-21) to NOT match this project's
            # actual Windows bundle -- fbsat59.spec places every
            # *.dll matched from the official Hamlib Windows release zip
            # (whatever its exact name turns out to be; the CI build
            # script itself only pattern-matches "*hamlib*.dll" rather
            # than hardcoding it) into the PyInstaller bundle root, which
            # for this project's onedir build is not necessarily right
            # next to the running .exe (PyInstaller 6+ defaults to a
            # nested _internal/ subdirectory). Search the filesystem for
            # the real file instead of guessing a bare name for ctypes'
            # DLL-search-path resolution.
            found_path = _find_hamlib_library_path()
            candidates = [found_path] if found_path else []
            if sys.platform == "win32":
                candidates += ["libhamlib-4.dll", "libhamlib.dll"]
            elif sys.platform == "darwin":
                candidates += ["libhamlib.4.dylib", "libhamlib.dylib"]
            else:
                candidates += ["libhamlib.so.4", "libhamlib.so"]

            # Python 3.8+ on Windows no longer searches PATH/CWD for a
            # loaded DLL's own dependencies unless the directory is
            # explicitly registered first -- confirmed live (2026-07-21):
            # _find_hamlib_library_path() correctly located the real file
            # (...\_internal\libhamlib-4.dll) yet ctypes.CDLL() on that
            # exact full path still failed, which "file not found" alone
            # cannot explain; a missing MinGW-runtime/libusb dependency
            # DLL sitting right next to it (in the same _internal/
            # directory, so not itself a location problem) is the likely
            # cause. main.py already does this for the normal `import
            # Hamlib` path elsewhere in the app, but that doesn't cover
            # this separate ctypes.CDLL() call.
            if sys.platform == "win32" and found_path:
                with contextlib.suppress(Exception):
                    os.add_dll_directory(os.path.dirname(found_path))  # type: ignore[attr-defined]

            lib = None
            last_load_error: Exception | None = None
            for name in candidates:
                try:
                    lib = ctypes.CDLL(name)
                    break
                except OSError as exc:
                    last_load_error = exc
            if lib is None:
                # POSIX only: None searches every already-loaded global
                # symbol table, a last-resort fallback if none of the
                # names above matched what's actually loaded.
                with contextlib.suppress(OSError):
                    lib = ctypes.CDLL(None)

            if lib is None:
                logger.warning(
                    "RigDirect: could not load Hamlib library via ctypes "
                    "(tried %s, last error: %s)",
                    candidates,
                    last_load_error,
                )
                return
            if not hasattr(lib, "rig_set_debug_filename"):
                logger.warning(
                    "RigDirect: rig_set_debug_filename not found in Hamlib library (tried %s)",
                    candidates,
                )
                return

            lib.rig_set_debug_filename.restype = ctypes.c_void_p
            lib.rig_set_debug_filename.argtypes = [ctypes.c_char_p]
            lib.rig_set_debug_filename(trace_path.encode("utf-8", "replace"))
            _hamlib_file_trace_enabled = True
            logger.info("RigDirect: Hamlib debug trace redirected to %s", trace_path)
        except Exception as exc:
            logger.warning("RigDirect: _hamlib_ensure_file_trace failed: %s", exc)


def _open_rig_with_retry(
    rig: Any,
    what: str,
    attempts: int = 3,
    retry_delay: float = 1.0,
) -> None:
    """Open a freshly constructed Hamlib *rig* session, retrying on failure.

    Originally written for a Windows-specific timing quirk (see git log for
    the full account), but a 2026-07-20 controlled test (same rig.open()
    failure with a verified-matching CI-V address on both PC and rig, and
    with either A2/A2 or A3/A3) shows the address is not the issue and the
    retries alone do not resolve it either -- root cause still open. Kept
    as a real improvement regardless (Hamlib itself has zero retry for
    this specific failure -- see below). Also ensures Hamlib's own debug
    trace is being captured to hamlib_trace.log (see
    _hamlib_ensure_file_trace()) so a failure here can be diagnosed from
    what Hamlib actually sent/received on the wire.

      - For Icom rigs, rig.open() is not a bare port open -- it performs
        its own internal CI-V "echo status" probe (rigs/icom/icom.c
        icom_get_usb_echo_off(), sent as part of icom_rig_open()) and
        fails the *entire* open() immediately if that first probe times
        out, with NO retry inside Hamlib for this specific case (the one
        retry Hamlib does have only covers a later step, a follow-up
        get_freq call, not this initial echo probe).
      - On Windows specifically, src/serial.c's serial_open() does an
        extra CreateFile-based "is this port already in use" check
        immediately before the real open, so the real open is preceded by
        an extra open/close handle churn on the same port that a plain
        pyserial-based probe never goes through.
    """
    _hamlib_ensure_file_trace()
    last_status = 0
    for attempt in range(1, attempts + 1):
        rig.open()
        last_status = rig.error_status
        if last_status == 0:
            return
        logger.warning(
            "%s: open() attempt %d/%d failed (Hamlib error %d)%s",
            what,
            attempt,
            attempts,
            last_status,
            "" if attempt == attempts else ", retrying...",
        )
        if attempt < attempts:
            with contextlib.suppress(Exception):
                rig.close()
            time.sleep(retry_delay)
    raise RigControlError(f"{what} failed after {attempts} attempts (Hamlib error {last_status})")


# ---------------------------------------------------------------------------
# Abstract base class — RigController
# ---------------------------------------------------------------------------


class RigController(ABC):
    """
    Abstract base class for transceiver control.

    All public methods are thread-safe (protected by an internal lock).
    Called from both the Qt UI thread and the tracking background thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = RigState.DISCONNECTED
        self._freq_state = FrequencyState()
        self._ptt_active: bool = False  # set by set_ptt(); freezes Doppler updates

    # -- Connection management --

    @abstractmethod
    def connect(self) -> bool:
        """Establish a connection. Returns True on success."""

    @abstractmethod
    def disconnect(self) -> None:
        """Disconnect."""

    @property
    def state(self) -> RigState:
        """Current connection state."""
        with self._lock:
            return self._state

    @property
    def is_connected(self) -> bool:
        """Whether currently connected."""
        return self.state == RigState.CONNECTED

    @property
    def is_satmode(self) -> bool:
        """True when the rig uses satmode (IC-9700/IC-9100 etc.). Overridden in subclasses."""
        return False

    @staticmethod
    def _freq_band(hz: float) -> str:
        """Return a coarse band label used for same-band detection."""
        if hz < 30e6:
            return "HF"
        if hz < 300e6:
            return "VHF"
        if hz < 3000e6:
            return "UHF"
        return "SHF"

    # -- Frequency and mode --

    @abstractmethod
    def set_frequency(self, freq_hz: float, vfo: str = "VFOA") -> bool:
        """Set the frequency in Hz."""

    @abstractmethod
    def get_frequency(self, vfo: str = "VFOA") -> float:
        """Return the current frequency in Hz. Returns -1.0 on error."""

    @abstractmethod
    def set_mode(self, mode: str, passband_hz: int = 0, vfo: str = "VFOA") -> bool:
        """Set the mode. mode is a SATNOGS format string ("FM", "SSB", etc.)."""

    @abstractmethod
    def get_mode(self, vfo: str = "VFOA") -> str:
        """Return the current mode as a SATNOGS format string."""

    # -- CTCSS / DCS tone --

    @abstractmethod
    def set_ctcss_tone(self, tone_hz: float) -> bool:
        """Set the CTCSS tone (0.0 to disable)."""

    @abstractmethod
    def set_dcs_code(self, code: int) -> bool:
        """Set the DCS code (0 to disable)."""

    # -- Custom CAT CTCSS --

    def send_ctcss_cat(  # noqa: B027
        self,
        tone_hz: float,
        cat_on_template: str,
        cat_off_template: str,
    ) -> None:
        """Send a custom CAT CTCSS command bypassing Hamlib's CTCSS API.

        Looks up tone_hz in CTCSS_TABLE to get the rig index, then formats
        cat_on_template with {tone=index} and splits on ';' to send each
        sub-command individually.  Sends cat_off_template when tone_hz <= 0
        or the tone is not in CTCSS_TABLE.  Default implementation is a no-op;
        subclasses override to send via their transport layer.
        """

    # -- VFO --

    @abstractmethod
    def set_vfo(self, vfo: str) -> bool:
        """Switch the active VFO ("VFOA" / "VFOB" / "Main" / "Sub")."""

    def set_vfo_frequencies(
        self,
        vfoa_hz: float | None,
        vfob_hz: float | None,
    ) -> bool:
        """Safely set the VFOA and VFOB frequencies.

        Can be overridden in subclasses. Default calls set_frequency sequentially.
        Returns False when not connected. Raises RigControlError on failure.
        """
        ok = True
        if vfoa_hz is not None:
            ok = self.set_frequency(vfoa_hz, "VFOA") and ok
        if vfob_hz is not None:
            ok = self.set_frequency(vfob_hz, "VFOB") and ok
        return ok

    def send_mode_only(self, dl_mode: str, ul_mode: str) -> None:
        """Set mode on both VFOs without affecting split state.

        Default implementation calls set_mode() for the downlink mode.
        Override in subclasses that support independent per-VFO mode setting.
        """
        self.set_mode(dl_mode)

    def apply_transponder_state(self, dl_mode: str, ul_mode: str, ctcss_hz: float) -> None:
        """Apply mode and CTCSS tone atomically on transponder selection.

        Default implementation calls send_mode_only then set_ctcss_tone
        sequentially in the calling thread.  Satmode subclasses (IC-9100,
        IC-9700 etc.) override this to avoid race conditions between the
        two operations.

        Called from a single background thread by main_window so that mode
        and CTCSS never interleave with each other or with Doppler updates.
        """
        self.send_mode_only(dl_mode, ul_mode)
        self.set_ctcss_tone(ctcss_hz)

    # -- PTT --

    def set_ptt(self, enabled: bool) -> bool:
        """Key or un-key the transmitter via CAT.

        Returns True on success, False when not connected or not supported.
        Default implementation is a no-op that returns False.
        Subclasses that support CAT PTT must override this method.

        The base class manages ``_ptt_active`` so that ``set_vfo_frequencies``
        can skip Doppler updates during the TX window without each subclass
        needing to handle it separately.
        """
        self._ptt_active = enabled
        return False

    # -- Utilities --

    @abstractmethod
    def get_rig_info(self) -> RigInfo | None:
        """Return connected rig info, or None when not connected."""

    def _mode_to_hamlib(self, mode: str) -> int:
        """Convert a SATNOGS mode string to a Hamlib constant. Unknown modes fall back to FM."""
        return MODE_MAP.get(mode, MODE_MAP["FM"])

    def _hamlib_to_mode(self, hamlib_mode: int) -> str:
        """Convert a Hamlib mode constant to a SATNOGS mode string."""
        reverse = {v: k for k, v in MODE_MAP.items()}
        return reverse.get(hamlib_mode, "FM")


# ---------------------------------------------------------------------------
# HamlibDirectController
# ---------------------------------------------------------------------------


class HamlibDirectController(RigController):
    """
    Transceiver controller that connects directly to a serial port via python-hamlib.

    Falls back to mock mode when Hamlib is not installed.
    """

    def __init__(
        self,
        model_id: int,
        port: str,
        baud_rate: int = 9600,
        data_bits: int = 8,
        stop_bits: int = 1,
        handshake: str = "None",
        civ_addr: str = "",
    ) -> None:
        """
        Args:
            model_id:  Hamlib rig model ID (e.g. IC-9700 = 3081)
            port:      Serial port ("/dev/ttyUSB0", "COM3", etc.)
            baud_rate: Baud rate
            data_bits: Data bits
            stop_bits: Stop bits
            handshake: Flow control ("None", "XONXOFF", "Hardware")
            civ_addr:  CI-V address override for Icom rigs (e.g. "0x65").
                       Empty string uses Hamlib's default for the model.
        """
        super().__init__()
        self._model_id = model_id
        self._port = port
        self._baud_rate = baud_rate
        self._data_bits = data_bits
        self._stop_bits = stop_bits
        self._handshake = handshake
        self._civ_addr = civ_addr.strip()
        self._rig: Any = None  # Hamlib.Rig instance or _MockRig
        self._hamlib: Any = None  # Hamlib module, set lazily in connect()
        self._last_dl_hz: float | None = None
        self._last_dl_update_time: float = 0.0
        self._last_ul_hz: float | None = None
        self._last_ul_update_time: float = 0.0
        # Which VFO our own last cross-band satmode write attempt (DL or UL)
        # left the rig's Hamlib-tracked "current VFO" on -- see
        # last_written_vfo_is_main(). Not maintained for same-band/non-satmode
        # writes (those don't feed the Lock read-skip logic).
        self._last_written_vfo: str | None = None
        self._ptt_active: bool = False
        self._satmode: bool = model_id in _SATMODE_RIG_IDS
        # True while IC-9100/9700 satmode is actually active on the rig.
        # Dynamically toggled: same-band pairs (V/V, U/U) use normal split
        # because IC-9100 satmode always assigns Main/Sub to different bands.
        self._satmode_active: bool = False
        self._current_dl_mode: str = ""  # updated by apply_transponder_state
        self._current_ul_mode: str = ""  # updated by apply_transponder_state
        self._current_ctcss_hz: float = 0.0  # updated by apply_transponder_state
        # Serialises multi-step rig command sequences (VFO switch + CTCSS etc.)
        # so they never interleave with the Doppler cycle's set_vfo_frequencies.
        self._rig_cmd_lock = threading.Lock()
        # Prevents _apply_ctcss_civ (pyserial) and connect() (Hamlib rig.open())
        # from opening the serial port simultaneously.
        self._port_lock = threading.Lock()
        # Last CTCSS tone set for satmode rigs (Hz). Re-applied in _satmode_enter.
        self._ctcss_tone_hz: float = 0.0
        # Transponder DL/UL frequencies stored at selection time for Stage-1 freq
        # pre-write in _apply_mode_and_ctcss_hamlib (IC-9100/9700 SAT mode anchor).
        self._transponder_dl_hz: float | None = None
        self._transponder_ul_hz: float | None = None
        # Stage-2 flag: after first UL write post-connect, re-send mode/CTCSS to
        # confirm correct VFO assignment once SAT mode band anchor is locked.
        self._pending_mode_ctcss: bool = False
        # Specific reason the last _apply_mode_and_ctcss_hamlib() call failed
        # (set on exception, read by apply_transponder_state() so the
        # RigControlError it raises carries a useful message instead of a
        # generic "apply failed").
        self._last_hamlib_error: str | None = None

    # -- Connection management --

    @property
    def is_satmode(self) -> bool:
        """True when this rig uses satmode (model_id in _SATMODE_RIG_IDS)."""
        return self._satmode

    def last_written_vfo_is_main(self) -> bool:
        """True if our own last cross-band satmode write (DL or UL) left the
        rig's Hamlib-tracked "current VFO" on Main -- False if it was Sub, or
        if nothing has been written yet (unknown, treated as unsafe).

        Used by the Lock (dial feedback) read for satmode Direct mode to
        decide whether it's safe to call get_frequency("Main") this cycle
        without risking an internal Hamlib VFO switch: for a rig with
        targetable_vfo == 0 (e.g. IC-9100), Hamlib's generic rig_get_freq()/
        rig_set_freq() each independently fall back to switching the active
        VFO whenever the requested VFO doesn't already match the current
        one -- and the cross-band satmode write path never restores Main
        after writing UL (confirmed live, 2026-07-22: this is exactly where
        a "Python not responding" hang was reproduced). Skipping the read
        for the one cycle right after a UL write avoids ever exercising that
        switch from the Lock read side, without touching the write path at
        all -- DL gets rewritten almost every cycle anyway, so this flips
        back to Main within about one Doppler cycle.
        """
        return self._last_written_vfo == "Main"

    def connect(self) -> bool:
        """Connect to the serial port."""
        with self._lock:
            if self._state == RigState.CONNECTED:
                return True
            self._state = RigState.CONNECTING

        try:
            if HAMLIB_AVAILABLE:
                import Hamlib as _H  # lazy — avoids Qt TLS collision at startup

                self._hamlib = _H
                rig = _H.Rig(self._model_id)
                # Hamlib 4.x: rigport is a SwigPyObject with no Python attributes;
                # use set_conf() instead of the old rig.state.rigport.pathname API.
                rig.set_conf("rig_pathname", self._port)
                rig.set_conf("serial_speed", str(self._baud_rate))
                rig.set_conf("data_bits", str(self._data_bits))
                rig.set_conf("stop_bits", str(self._stop_bits))
                if self._civ_addr:
                    addr = normalize_civ_addr(self._civ_addr)
                    rig.set_conf("civaddr", addr)
                    logger.info("RigDirect: CI-V address set to %s", addr)
                if self._satmode and hasattr(_H, "RIG_FUNC_SATMODE"):
                    # Satmode rigs: open once, send set_func(SATMODE, 1) so Hamlib
                    # uses the correct CI-V per model (16 5A for IC-9100/9700,
                    # 1A 07 for IC-910H), then close and reopen.  On the second
                    # open Hamlib reads satmode=1 and sets cache->satmode=1,
                    # which allows set_freq(VFO_TX) for UL writes.
                    #
                    # Delays here are deliberately more generous than the
                    # ~0.1-0.3s that sufficed in Linux testing: reopening a
                    # COM port too soon after closing it is a known source of
                    # silent failures on Windows USB-serial drivers, and
                    # unlike the per-second Doppler write loop this sequence
                    # only runs once per Connect click, so extra settling
                    # time is cheap. Each step's outcome (rig.error_status,
                    # NOT the call's own return value -- see _check_rig_ok())
                    # is also checked now (raises RigControlError on failure)
                    # so a failure here surfaces to the status bar instead of
                    # leaving the rig silently un-configured.
                    _open_rig_with_retry(rig, "RigDirect satmode entry: open()")
                    time.sleep(0.5)
                    rig.set_func(_H.RIG_FUNC_SATMODE, 1)
                    _check_rig_ok(rig, "RigDirect satmode entry: set_func(SATMODE,1)")
                    time.sleep(0.2)
                    rig.close()
                    time.sleep(0.3)
                with self._port_lock:
                    _open_rig_with_retry(rig, "RigDirect: reopen after satmode entry")
                    # IC-9700 does not correctly read back satmode=1 during open(),
                    # leaving cache->satmode=0.  A second set_func call after open()
                    # forces cache->satmode=1 so that VFO_MAIN/VFO_SUB are routed
                    # correctly for subsequent set_freq / set_mode calls.
                    # IC-9100/IC-910H/IC-821H must NOT receive this extra call —
                    # sending set_func(SATMODE,1) twice breaks those rigs (confirmed).
                    if self._satmode and self._model_id in _SATMODE_USE_VFO_SUB:
                        time.sleep(0.2)
                        rig.set_func(_H.RIG_FUNC_SATMODE, 1)
                        _check_rig_ok(rig, "RigDirect: IC-9700 extra set_func(SATMODE,1)")
                        time.sleep(0.2)
                        logger.info("RigDirect: IC-9700 extra set_func(SATMODE,1) to fix cache")
                self._rig = rig
            else:
                self._rig = _MockRig(self._model_id)

            self._last_dl_hz = None
            self._last_dl_update_time = 0.0
            self._last_ul_hz = None
            self._last_ul_update_time = 0.0
            self._last_written_vfo = None
            self._init_split()

            with self._lock:
                self._state = RigState.CONNECTED
            logger.info("RigDirect: connected to %s (model %d)", self._port, self._model_id)
            return True

        except Exception as exc:
            with self._lock:
                self._state = RigState.ERROR
            logger.error("RigDirect: connect failed — %s", exc)
            return False

    def disconnect(self) -> None:
        """Disconnect from the serial port."""
        with self._lock:
            if self._state == RigState.DISCONNECTED:
                return
        try:
            if self._rig is not None:
                self._rig.close()
        except Exception as exc:
            logger.warning("RigDirect: disconnect error — %s", exc)
        finally:
            self._rig = None
            self._hamlib = None
            self._last_dl_hz = None
            self._last_dl_update_time = 0.0
            self._last_ul_hz = None
            self._last_written_vfo = None
            with self._lock:
                self._state = RigState.DISCONNECTED

    # -- Frequency and mode --

    def set_frequency(self, freq_hz: float, vfo: str = "VFOA") -> bool:
        """Set the frequency in Hz."""
        if not self.is_connected or self._rig is None:
            return False
        try:
            hamlib_vfo = self._vfo_str_to_const(vfo)
            self._rig.set_freq(hamlib_vfo, freq_hz)
            with self._lock:
                self._freq_state.freq_hz = freq_hz
            return True
        except Exception as exc:
            logger.error("RigDirect.set_frequency: %s", exc)
            return False

    def get_frequency(self, vfo: str = "VFOA") -> float:
        """Return the current frequency in Hz.

        Serialised through _rig_cmd_lock, the same lock set_vfo_frequencies()
        holds, so a Lock (dial feedback) read never interleaves on the wire
        with a concurrent write (e.g. a user-triggered mode/CTCSS change).
        """
        if not self.is_connected or self._rig is None:
            return -1.0
        try:
            with self._rig_cmd_lock:
                hamlib_vfo = self._vfo_str_to_const(vfo)
                return float(self._rig.get_freq(hamlib_vfo))
        except Exception as exc:
            logger.error("RigDirect.get_frequency: %s", exc)
            return -1.0

    def set_mode(self, mode: str, passband_hz: int = 0, vfo: str = "VFOA") -> bool:
        """Set the mode and passband."""
        if not self.is_connected or self._rig is None:
            return False
        try:
            hamlib_mode = self._mode_to_hamlib(mode)
            hamlib_vfo = self._vfo_str_to_const(vfo)
            # Python Hamlib binding: set_mode(mode, passband[, vfo]) — vfo is last
            self._rig.set_mode(hamlib_mode, passband_hz, hamlib_vfo)
            with self._lock:
                self._freq_state.mode = mode
                self._freq_state.passband_hz = passband_hz
            return True
        except Exception as exc:
            logger.error("RigDirect.set_mode: %s", exc)
            return False

    def get_mode(self, vfo: str = "VFOA") -> str:
        """Return the current mode as a SATNOGS format string."""
        if not self.is_connected or self._rig is None:
            return "FM"
        try:
            hamlib_vfo = self._vfo_str_to_const(vfo)
            mode, _ = self._rig.get_mode(hamlib_vfo)
            return self._hamlib_to_mode(mode)
        except Exception as exc:
            logger.error("RigDirect.get_mode: %s", exc)
            return "FM"

    def set_ctcss_tone(self, tone_hz: float) -> bool:
        """Set CTCSS tone for satmode rigs via Hamlib (cross-platform).

        When the rig is NOT connected (before Doppler), opens a fresh Hamlib
        rig session to send VFO-Sub CTCSS commands.  When the rig IS connected
        (Doppler running), the port is held by Hamlib; _satmode_enter
        re-applies CTCSS after satmode is activated so no extra action is
        needed here.

        For non-satmode rigs, the standard Hamlib set_ctcss_tone / set_func
        path is used (works for FTX-1F, FT-991A etc.).
        """
        logger.info(
            "RigDirect.set_ctcss_tone: %.1fHz satmode=%s connected=%s",
            tone_hz,
            self._satmode,
            self.is_connected,
        )
        self._ctcss_tone_hz = tone_hz
        with self._lock:
            self._freq_state.ctcss_tone = tone_hz

        if self._satmode:
            if not self.is_connected:
                # Port is free: apply via Hamlib (cross-platform).
                return self._apply_mode_and_ctcss_hamlib(
                    self._current_dl_mode, self._current_ul_mode, tone_hz
                )
            # Port held by Hamlib; _satmode_enter will apply CTCSS after connect.
            logger.info("RigDirect.set_ctcss_tone: connected — deferred to _satmode_enter")
            return True

        # Non-satmode rig: use Hamlib binding on current VFO.
        if self._rig is None:
            # Port is free (e.g. transponder selected before Connect is
            # pressed) — open a short-lived session, same pattern as
            # send_mode_only(), instead of silently doing nothing.
            return self._apply_ctcss_hamlib_standalone(tone_hz)
        if self._hamlib is None:
            return True
        try:
            _H = self._hamlib
            tone_int = int(round(abs(tone_hz) * 10))
            # Icom CI-V over USB-serial needs a short gap between back-to-back
            # transactions (same reason _apply_ctcss_civ_via_send_raw() sleeps
            # 0.15s between raw frames) — firing set_ctcss_tone and set_func
            # with no delay let the tone-frequency write land but silently
            # dropped the TONE-enable write on an IC-705 (confirmed live).
            self._rig.set_ctcss_tone(_H.RIG_VFO_CURR, tone_int)
            time.sleep(0.15)
            self._rig.set_func(_H.RIG_VFO_CURR, _H.RIG_FUNC_TONE, 1 if tone_hz > 0 else 0)
            return True
        except Exception as exc:
            logger.error("RigDirect.set_ctcss_tone (non-satmode): %s", exc)
            return False

    def _apply_ctcss_hamlib_standalone(self, tone_hz: float) -> bool:
        """Set CTCSS via a short-lived Hamlib session for a disconnected generic rig.

        Mirrors send_mode_only(): opens its own Hamlib session (independent of
        self._rig), applies the tone to VFO-B (the UL/TX vfo in the generic
        split convention), then reselects VFO-A so the rig's main display
        doesn't stay on the tone-setting VFO. Best-effort; errors are logged
        and swallowed, matching the rest of the transponder-selection path.

        Delays between each Hamlib call are required — confirmed live on an
        IC-705: firing set_vfo/set_ctcss_tone/set_func back-to-back with no
        gap let the tone frequency land (readback matched) but silently
        dropped the TONE-enable command. Same reasoning as the 0.15s sleeps
        in _apply_ctcss_civ_via_send_raw().
        """
        if not HAMLIB_AVAILABLE:
            return True
        rig: Any = None
        with self._port_lock:
            try:
                import Hamlib as _H

                rig = _H.Rig(self._model_id)
                rig.set_conf("rig_pathname", self._port)
                rig.set_conf("serial_speed", str(self._baud_rate))
                if self._civ_addr:
                    rig.set_conf("civaddr", normalize_civ_addr(self._civ_addr))
                tone_int = int(round(abs(tone_hz) * 10))
                enable = tone_hz > 0
                _open_rig_with_retry(rig, "CTCSS standalone: open()")
                vfo_b = int(_H.RIG_VFO_B)
                vfo_a = int(_H.RIG_VFO_A)
                rig.set_vfo(vfo_b)
                time.sleep(0.15)
                rig.set_ctcss_tone(vfo_b, tone_int)
                time.sleep(0.15)
                rig.set_func(vfo_b, _H.RIG_FUNC_TONE, 1 if enable else 0)
                time.sleep(0.15)
                rig.set_vfo(vfo_a)
                time.sleep(0.15)
                rig.set_func(vfo_a, _H.RIG_FUNC_TONE, 0)
                logger.info("RigDirect: CTCSS standalone applied %.1fHz", tone_hz)
                return True
            except Exception as exc:
                logger.error("RigDirect._apply_ctcss_hamlib_standalone: %s", exc)
                return False
            finally:
                if rig is not None:
                    with contextlib.suppress(Exception):
                        rig.close()

    def _civ_addr_int(self) -> int:
        """Return the CI-V rig address as an integer (default 0x65 for IC-9100)."""
        if self._civ_addr:
            try:
                return int(normalize_civ_addr(self._civ_addr), 16)
            except ValueError:
                pass
        return 0x65

    @staticmethod
    def _civ_bcd_tone(tone_hz: float) -> bytes:
        """Encode CTCSS tone Hz as 2-byte BCD for IC-9100 CI-V command 1B 00.

        IC-9100 encodes the tone as 4 BCD digits (67.0 Hz -> 0670 -> 0x06 0x70).
        """
        val = int(round(tone_hz * 10))
        high = val // 100
        low = val % 100
        return bytes([(high // 10) << 4 | (high % 10), (low // 10) << 4 | (low % 10)])

    def _apply_ctcss_civ(self, tone_hz: float) -> bool:
        """Send CI-V commands to set CTCSS on IC-9100 Sub band via pyserial.

        Replicates the rigctl sequence confirmed to work:
          V Sub -> set_ctcss_tone 670 -> U TONE 1/0 -> V Main

        CI-V frames (example civ_addr=0x65, ctrl=0xE0):
          Select Sub:    FE FE 65 E0 07 D1 FD
          Set tone freq: FE FE 65 E0 1B 00 <bcd> FD
          TONE ON/OFF:   FE FE 65 E0 16 42 01/00 FD
          Select Main:   FE FE 65 E0 07 D0 FD
        """
        try:
            import serial
        except ImportError:
            logger.warning("RigDirect: pyserial not available — cannot apply CTCSS via CI-V")
            return False

        civ = self._civ_addr_int()
        ctrl = 0xE0

        def frame(*payload: int) -> bytes:
            return bytes([0xFE, 0xFE, civ, ctrl, *payload, 0xFD])

        # IC-9100 CI-V mode bytes for command 0x06
        _civ_mode: dict[str, int] = {
            "FM": 0x05,
            "DIGITALVOICE": 0x05,
            "AFSK": 0x05,
            "USB": 0x01,
            "SSB": 0x01,
            "BPSK": 0x01,
            "LSB": 0x00,
            "CW": 0x03,
            "CW-R": 0x07,
            "AM": 0x02,
        }
        enable = tone_hz > 0
        tone_bcd = self._civ_bcd_tone(tone_hz) if enable else b"\x00\x00"
        tone_byte = 0x01 if enable else 0x00
        dl_civ = _civ_mode.get(self._current_dl_mode, 0x05)
        ul_civ = _civ_mode.get(self._current_ul_mode, 0x05)

        try:
            with self._port_lock:
                ser = serial.Serial(
                    self._port,
                    self._baud_rate,
                    bytesize=self._data_bits,
                    stopbits=self._stop_bits,
                    timeout=0.5,
                )
                logger.info(
                    "RigDirect: CI-V CTCSS %.1fHz enable=%s civ=0x%02X port=%s dl=%s ul=%s",
                    tone_hz,
                    enable,
                    civ,
                    self._port,
                    self._current_dl_mode,
                    self._current_ul_mode,
                )

                def send(f: bytes) -> None:
                    for attempt in range(3):
                        ser.write(f)
                        ser.flush()
                        resp = ser.read_until(b"\xfd")
                        if resp.endswith(b"\xfd"):
                            return
                        logger.warning(
                            "RigDirect: CI-V no ACK (attempt %d/3): %s",
                            attempt + 1,
                            f.hex(),
                        )
                    raise RigControlError(f"CI-V no ACK after 3 attempts: {f.hex()}")

                try:
                    send(frame(0x16, 0x5A, 0x00))  # SAT MODE OFF — reset state (16 5A = SAT mode)
                    send(frame(0x16, 0x5A, 0x01))  # SAT MODE ON — re-enter clean
                    send(frame(0x07, 0xD1))  # Select Sub
                    send(frame(0x1B, 0x00, *tone_bcd))  # Tone freq
                    send(frame(0x16, 0x42, tone_byte))  # TONE ON/OFF on Sub
                    send(frame(0x06, ul_civ))  # UL mode on Sub
                    send(frame(0x07, 0xD0))  # Select Main
                    send(frame(0x16, 0x42, 0x00))  # TONE OFF on Main (prevents bleed-through)
                    send(frame(0x06, dl_civ))  # DL mode on Main
                    logger.info("RigDirect: CI-V CTCSS applied OK")
                finally:
                    ser.close()
            return True
        except Exception as exc:
            logger.error("RigDirect._apply_ctcss_civ: %s", exc)
            return False

    def set_dcs_code(self, code: int) -> bool:
        """Set the DCS code. Pass code=0 to disable."""
        if not self.is_connected or self._rig is None:
            return False
        if self._hamlib is None:
            with self._lock:
                self._freq_state.dcs_code = code
            return True
        try:
            if code > 0:
                self._rig.set_func(
                    self._hamlib.RIG_VFO_CURR,
                    self._hamlib.RIG_FUNC_TSQL,
                    1,
                )
                self._rig.set_level(
                    self._hamlib.RIG_VFO_CURR,
                    self._hamlib.RIG_LEVEL_CTCSS_SQL,
                    code,
                )
            else:
                self._rig.set_func(
                    self._hamlib.RIG_VFO_CURR,
                    self._hamlib.RIG_FUNC_TSQL,
                    0,
                )
            with self._lock:
                self._freq_state.dcs_code = code
            return True
        except Exception as exc:
            logger.error("RigDirect.set_dcs_code: %s", exc)
            return False

    def set_vfo(self, vfo: str) -> bool:
        """Switch the active VFO."""
        if not self.is_connected or self._rig is None:
            return False
        try:
            self._rig.set_vfo(self._vfo_str_to_const(vfo))
            return True
        except Exception as exc:
            logger.error("RigDirect.set_vfo: %s", exc)
            return False

    def set_ptt(self, enabled: bool) -> bool:
        """Key or un-key the transmitter via Hamlib direct binding."""
        super().set_ptt(enabled)  # updates _ptt_active
        if not self.is_connected or self._rig is None:
            return False
        try:
            ptt_val = self._hamlib.RIG_PTT_ON if enabled else self._hamlib.RIG_PTT_OFF
            self._rig.set_ptt(self._hamlib.RIG_VFO_CURR, ptt_val)
            return True
        except Exception as exc:
            logger.error("RigDirect.set_ptt(%s): %s", enabled, exc)
            return False

    def set_vfo_frequencies(
        self,
        vfoa_hz: float | None,
        vfob_hz: float | None,
    ) -> bool:
        """Set DL and UL frequencies with 1 Hz delta suppression.

        Icom satmode rigs (IC-9700 etc.) use RIG_VFO_MAIN for both set_freq
        and set_split_freq; the firmware routes DL→Main and UL→Sub internally.
        Generic rigs use VFOA for DL and set_split_freq for UL (VFOB/split TX).
        Skips the command when the frequency has not changed by 1 Hz or more,
        or when the argument is None.
        """
        if not self.is_connected or self._rig is None:
            return False
        if self._ptt_active:
            return True
        with self._rig_cmd_lock:
            return self._set_vfo_frequencies_locked(vfoa_hz, vfob_hz)

    def _set_vfo_frequencies_locked(
        self,
        vfoa_hz: float | None,
        vfob_hz: float | None,
    ) -> bool:
        """Inner implementation of set_vfo_frequencies; caller must hold _rig_cmd_lock."""
        try:
            if self._satmode:
                # IC-9100/9700 satmode: satmode routes Main=RX(DL) and Sub=TX(UL).
                # RIG_VFO_SUB_A (0x00200000) bypasses vfo_fixup so ic9700_set_vfo
                # sends CI-V 07 d1 (Sub Band select) rather than 07 01 (VFO-B of
                # current band) — the latter was the root cause of Sub stuck at 7 MHz.
                #
                # IC-9100 hardware constraint: satmode ALWAYS assigns Main and Sub to
                # DIFFERENT bands.  Same-band satmode (V/V FM, ISS APRS etc.) is not
                # supported by IC-9100 firmware — the rig forces Sub to the opposite
                # band.  For same-band pairs we fall back to conventional VFO-A/B split
                # to get correct frequencies (display alternates during UL updates but
                # at most every 5 s, which is acceptable).
                _H = self._hamlib
                rx_vfo = self._vfo_str_to_const("VFOA")

                # Detect same-band: when DL and UL are in the same frequency band
                # (both VHF, both UHF, etc.) satmode cannot work correctly.
                _is_same_band = (
                    vfoa_hz is not None
                    and vfob_hz is not None
                    and self._freq_band(vfoa_hz) == self._freq_band(vfob_hz)
                )

                if _is_same_band:
                    # Same-band fallback: exit satmode once and use VFO-A/B split.
                    if self._satmode_active:
                        self._satmode_exit()
                    tx_vfo = self._vfo_str_to_const("VFOB")
                    is_fm = self._current_dl_mode in ("FM", "AFSK", "DIGITALVOICE")
                    now = time.monotonic()
                    if vfoa_hz is not None:
                        last_dl = self._last_dl_hz
                        elapsed_dl = now - self._last_dl_update_time
                        # FM same-band: Hamlib icom backend prefixes each set_freq(VFOA)
                        # with a CI-V VFO-select frame (07 00) which causes IC-9100 to
                        # flicker the main display every call.  FM capture range (±5 kHz)
                        # covers ISS max Doppler (±3.5 kHz), so coarse DL updates suffice.
                        _DL_THRESH = 2000.0 if is_fm else 1.0
                        _DL_MAX_S = 60.0 if is_fm else 0.0
                        if (
                            last_dl is None
                            or abs(vfoa_hz - last_dl) >= _DL_THRESH
                            or (is_fm and elapsed_dl >= _DL_MAX_S)
                        ):
                            logger.info("RigDirect same-band DL: set_freq(VFOA, %d)", int(vfoa_hz))
                            self._rig.set_freq(rx_vfo, int(vfoa_hz))
                            _check_rig_ok(self._rig, "same-band DL set_freq(VFOA)")
                            self._last_dl_hz = vfoa_hz
                            self._last_dl_update_time = now
                    if vfob_hz is not None:
                        last_ul = self._last_ul_hz
                        elapsed = now - self._last_ul_update_time
                        # FM same-band split: VFO-B switch causes display flicker on
                        # IC-9100.  FM/AFSK capture range (±5 kHz) exceeds ISS max
                        # Doppler (±3.5 kHz at 145 MHz), so infrequent UL updates are
                        # fine.  2 kHz threshold + 60 s ceiling minimises flicker while
                        # keeping UL within the capture range throughout the pass.
                        _UL_THRESH = 2000.0 if is_fm else 20.0
                        _UL_MAX_S = 60.0 if is_fm else 15.0
                        if (
                            last_ul is None
                            or abs(vfob_hz - last_ul) >= _UL_THRESH
                            or elapsed >= _UL_MAX_S
                        ):
                            # Use set_freq(VFOB) instead of set_split_freq: Hamlib's
                            # set_split_freq checks an internal tx_freq cache populated
                            # by set_split_vfo and may skip the actual CI-V command
                            # ("freq set not needed") even when VFO-B on the rig still
                            # holds a stale value from a previous session.
                            logger.info("RigDirect same-band UL: set_freq(VFOB, %d)", int(vfob_hz))
                            self._rig.set_freq(tx_vfo, int(vfob_hz))
                            _check_rig_ok(self._rig, "same-band UL set_freq(VFOB)")
                            self._last_ul_hz = vfob_hz
                            self._last_ul_update_time = now
                            # Restore VFO-A as the displayed VFO so IC-9100 shows
                            # DL (Main) after the UL write.  set_freq(VFOB) leaves
                            # the icom backend's internal CURR on VFO-B; set_vfo()
                            # sends CI-V 07 00 explicitly to switch the display back
                            # to VFO-A without re-writing the DL frequency.
                            logger.info("RigDirect same-band: set_vfo(VFOA) to restore DL display")
                            self._rig.set_vfo(rx_vfo)
                else:
                    # Cross-band: SAT mode is active (entered via CI-V 16 5A 01
                    # before rig.open()).  Hamlib cache.satmode stays 0, so
                    # set_freq(VFO_TX) writes Sub (TX/UL) without ic9700_set_vfo
                    # rejection.  No satmode toggle needed.
                    main_vfo = int(_H.RIG_VFO_MAIN)
                    # IC-9700 needs RIG_VFO_SUB; IC-9100/910H/821H need RIG_VFO_TX.
                    if self._model_id in _SATMODE_USE_VFO_SUB:
                        vfo_tx = int(_H.RIG_VFO_SUB)
                    else:
                        vfo_tx = int(_H.RIG_VFO_TX)
                    if vfoa_hz is not None:
                        last_dl = self._last_dl_hz
                        if last_dl is None or self._freq_band(vfoa_hz) != self._freq_band(last_dl):
                            # Band change or first tick: write DL and reset UL
                            # cache so VFO_TX fires on the very next iteration.
                            logger.info(
                                "RigDirect satmode DL (band/init): set_freq(MAIN, %d)", int(vfoa_hz)
                            )
                            self._rig.set_freq(main_vfo, int(vfoa_hz))
                            _check_rig_ok(self._rig, "satmode DL set_freq(MAIN)")
                            self._last_dl_hz = vfoa_hz
                            self._last_ul_hz = None
                            self._last_ul_update_time = 0.0
                            self._last_written_vfo = "Main"
                        elif abs(vfoa_hz - last_dl) >= 1.0:
                            logger.info("RigDirect satmode DL: set_freq(MAIN, %d)", int(vfoa_hz))
                            self._rig.set_freq(main_vfo, int(vfoa_hz))
                            _check_rig_ok(self._rig, "satmode DL set_freq(MAIN)")
                            self._last_dl_hz = vfoa_hz
                            self._last_written_vfo = "Main"

                    if vfob_hz is None:
                        logger.debug(
                            "RigDirect satmode: vfob_hz is None — no uplink defined, UL skipped"
                        )
                    else:
                        last_ul = self._last_ul_hz
                        now = time.monotonic()
                        elapsed = now - self._last_ul_update_time
                        is_fm = self._current_dl_mode in ("FM", "DIGITALVOICE")
                        _UL_THRESH = 10.0 if is_fm else 20.0
                        _UL_MAX_S = 5.0 if is_fm else 15.0
                        if (
                            last_ul is None
                            or abs(vfob_hz - last_ul) >= _UL_THRESH
                            or elapsed >= _UL_MAX_S
                        ):
                            vfo_name = (
                                "VFO_SUB" if self._model_id in _SATMODE_USE_VFO_SUB else "VFO_TX"
                            )
                            logger.info(
                                "RigDirect satmode UL: set_freq(%s, %d)", vfo_name, int(vfob_hz)
                            )
                            was_first_ul = last_ul is None
                            # No local try/except here (there used to be one) --
                            # a failure now propagates to the shared handler
                            # below via RigControlError instead of being
                            # silently logged and forgotten. This also means
                            # _last_ul_hz is only cached on actual success, so
                            # a persistently failing UL keeps retrying every
                            # cycle instead of being (incorrectly) considered
                            # "already applied" after the first failed attempt.
                            self._rig.set_freq(vfo_tx, int(vfob_hz))
                            _check_rig_ok(self._rig, f"satmode UL set_freq({vfo_name})")
                            self._last_ul_hz = vfob_hz
                            self._last_ul_update_time = now
                            self._last_written_vfo = "Sub"
                            if was_first_ul and self._pending_mode_ctcss:
                                self._pending_mode_ctcss = False
                                self._resend_mode_ctcss_via_rig()

            else:
                rx_vfo = self._vfo_str_to_const("VFOA")
                dl_written = False
                if vfoa_hz is not None:
                    last_dl = self._last_dl_hz
                    if last_dl is None or abs(vfoa_hz - last_dl) >= 1.0:
                        self._rig.set_freq(rx_vfo, int(vfoa_hz))
                        self._last_dl_hz = vfoa_hz
                        dl_written = True
                if vfob_hz is not None:
                    last_ul = self._last_ul_hz
                    if last_ul is None or abs(vfob_hz - last_ul) >= 1.0:
                        if self._model_id in _FT991_DIRECT_MODEL_IDS:
                            # Hamlib set_split_freq returns -11 (ENAVAIL) for
                            # FT-991/991A and silently does nothing.  Send the
                            # raw CAT FB command directly instead.
                            import os as _os

                            cmd = f"FB{int(vfob_hz):09d};".encode()
                            _fd = _os.open(self._port, _os.O_WRONLY | _os.O_NOCTTY | _os.O_NONBLOCK)
                            try:
                                _os.write(_fd, cmd)
                            finally:
                                _os.close(_fd)
                            logger.debug("RigDirect FT-991 UL raw FB: %d Hz", int(vfob_hz))
                        else:
                            # Hamlib set_split_freq is unreliable on generic
                            # rigs (e.g. IC-705): passing either the RX vfo or
                            # RIG_VFO_CURR ends up overwriting VFOA instead of
                            # VFOB, or silently doing nothing (confirmed via
                            # scripts/test_ic705_split.py 2026-07-06 — same
                            # root cause already documented for the satmode
                            # same-band fallback above).  Target VFO-B
                            # directly instead.
                            #
                            # Every other Icom CI-V write sequence in this
                            # file (mode/CTCSS setup) separates commands with
                            # 0.05s+ sleeps because Icom rigs drop closely-
                            # spaced CI-V frames.  This DL/UL/restore triple
                            # had none, which let the rig silently miss the
                            # restore despite every call reporting success
                            # (confirmed on an IC-705).
                            if dl_written:
                                time.sleep(0.05)
                            tx_vfo = self._vfo_str_to_const("VFOB")
                            self._rig.set_freq(tx_vfo, int(vfob_hz))
                            time.sleep(0.05)
                            # Icom CI-V backends (e.g. IC-705) leave their
                            # internal CURR on VFO-B after this call, so the
                            # rig keeps displaying UL as the main frequency.
                            # Same quirk as the satmode same-band fallback
                            # above — explicitly reselect VFO-A to restore
                            # the DL display.
                            #
                            # FTX-1F must NOT receive this: this branch is
                            # shared with IC-705 (confirmed 2026-07-06, commit
                            # 6885275, "Icom CI-V backends (confirmed on
                            # IC-705)"), but for FTX-1F set_vfo(VFOA) sends
                            # raw CAT "VS0;" (active-VFO select) -- a command
                            # ftx1_vfo.c documents as independent from "FT"
                            # (TX-VFO assignment). FBSAT59 deliberately never
                            # sends FTX-1F's official split ("ST") command
                            # (see _init_split()'s FT1;/FT0; raw-CAT bypass),
                            # so the rig has no split state telling it these
                            # two are unrelated -- confirmed live (2026-07-20)
                            # that "VS0;" resets TX from Sub back to Main,
                            # undoing _init_split()'s "FT1;" on every UL
                            # write. Skip the restore entirely for FTX-1F.
                            if self._model_id not in _FTX1_MODEL_IDS:
                                self._rig.set_vfo(rx_vfo)
                        self._last_ul_hz = vfob_hz
            return True
        except RigControlError as exc:
            # Explicit Hamlib-return-code failure from a _check_rig_ok() call
            # above -- re-raise so it reaches HamlibDirectController's caller
            # (main_window.py's _rig_send(), which already emits it to the
            # status bar) instead of being silently logged and forgotten,
            # unlike an unexpected/unclassified exception (below).
            logger.error("RigDirect.set_vfo_frequencies: %s", exc)
            raise
        except Exception as exc:
            logger.error("RigDirect.set_vfo_frequencies: %s", exc)
            return False

    def send_mode_only(self, dl_mode: str, ul_mode: str) -> None:
        """Set mode on the DL (RX) and UL (TX) VFOs.

        Opens a dedicated short-lived serial connection so that the mode can be
        set even when the main tracking connection has already been disconnected
        — mirroring HamlibNetController which opens a fresh TCP socket per call.
        Icom satmode rigs use RIG_VFO_MAIN/SUB; generic rigs use RIG_VFO_A/B.
        Silently ignores all errors (best-effort).

        Icom satmode rigs use RIG_VFO_MAIN for DL and RIG_VFO_SUB for UL;
        generic rigs use RIG_VFO_A and RIG_VFO_B respectively.
        """
        self._current_dl_mode = dl_mode
        self._current_ul_mode = ul_mode
        logger.info("RigDirect: send_mode_only dl=%s ul=%s", dl_mode, ul_mode)
        if not HAMLIB_AVAILABLE:
            return
        rig: Any = None
        try:
            import Hamlib as _H  # lazy — avoids Qt TLS collision at startup

            # Build mode map from real Hamlib constants (available after import).
            # Python binding: set_mode(mode, passband[, vfo]) — vfo is the last arg.
            hamlib_mode: dict[str, int] = _build_live_hamlib_mode_map(_H)
            dl_hamlib = hamlib_mode.get(dl_mode, _H.RIG_MODE_FM)
            ul_hamlib = hamlib_mode.get(ul_mode, _H.RIG_MODE_FM)
            # For satmode rigs: use Main/Sub VFOs only while satmode is active
            # (cross-band operation).  When satmode has been exited (same-band
            # duplex path called _satmode_exit), use VFOA/VFOB so that mode is
            # set on the correct split VFOs.  Using _satmode_active avoids the
            # earlier _last_ul_hz=None race that selected wrong VFOs on the very
            # first mode-set call after connect.
            _use_satmode_vfo = self._satmode and self._satmode_active
            dl_vfo = _H.RIG_VFO_MAIN if _use_satmode_vfo else _H.RIG_VFO_A
            # RIG_VFO_SUB (ic9700_set_vfo: CI-V 07 D1 = Sub Band select) is the
            # correct VFO for satmode UL mode setting.  RIG_VFO_SUB_A is invalid
            # in satmode and ic9700_set_vfo rejects it with EINVAL.
            ul_vfo = int(_H.RIG_VFO_SUB) if _use_satmode_vfo else _H.RIG_VFO_B
            rig = _H.Rig(self._model_id)
            rig.set_conf("rig_pathname", self._port)
            rig.set_conf("serial_speed", str(self._baud_rate))
            if self._civ_addr:
                rig.set_conf("civaddr", normalize_civ_addr(self._civ_addr))
            with self._port_lock:
                _open_rig_with_retry(rig, "RigDirect send_mode_only: open()")
                if _use_satmode_vfo:
                    # Icom satmode rigs support set_mode(mode, 0, VFO_MAIN/SUB_A)
                    # directly — no VFO switch needed.
                    rig.set_mode(dl_hamlib, 0, dl_vfo)
                    rig.set_mode(ul_hamlib, 0, ul_vfo)
                else:
                    # Non-satmode rigs (FTX-1F, FT-991A etc.): set_mode with an
                    # explicit VFO_B argument hangs (~39 s Hamlib timeout) because
                    # the backend does not support per-VFO mode setting without
                    # switching the active VFO first.  Mirror the rigctld sequence:
                    #   V VFOB → set_mode(UL) → V VFOA → set_mode(DL)
                    vfo_b = int(_H.RIG_VFO_B)
                    vfo_a = int(_H.RIG_VFO_A)
                    rig.set_vfo(vfo_b)
                    rig.set_mode(ul_hamlib, 0)
                    rig.set_vfo(vfo_a)
                    rig.set_mode(dl_hamlib, 0)
            logger.info("RigDirect: send_mode_only done")
        except Exception as exc:
            logger.error("RigDirect.send_mode_only: %s", exc)
        finally:
            if rig is not None:
                with contextlib.suppress(Exception):
                    rig.close()

    def _apply_mode_and_ctcss_hamlib(self, dl_mode: str, ul_mode: str, ctcss_hz: float) -> bool:
        """Set mode and CTCSS on satmode rigs via Hamlib (cross-platform).

        Opens a fresh Hamlib session (satmode=1 is already set in the rig
        hardware from a prior connect() or satmode_warmup()).  The second
        rig.open() reads satmode=1 from the rig → cache->satmode=1 is
        established, which allows set_mode(VFO_MAIN/SUB) and correct CI-V
        routing.

        Sequence:
          open()
          set_mode(dl_hamlib, 0, VFO_MAIN)
          set_mode(ul_hamlib, 0, VFO_SUB)
          set_vfo(VFO_SUB)
          set_ctcss_tone(VFO_SUB, deci_hz)  [tone=0 to clear]
          set_func(FUNC_TONE, 1/0)           [on Sub]
          set_vfo(VFO_MAIN)
          set_func(FUNC_TONE, 0)             [clear any bleed on Main]
          close()

        Returns True on success, False on error.
        """
        try:
            import Hamlib as _H  # noqa: PLC0415
        except ImportError:
            self._last_hamlib_error = "Hamlib not available"
            logger.error("RigDirect._apply_mode_and_ctcss_hamlib: Hamlib not available")
            return False

        hamlib_mode: dict[str, int] = _build_live_hamlib_mode_map(_H)
        dl_hamlib = hamlib_mode.get(dl_mode, _H.RIG_MODE_FM)
        ul_hamlib = hamlib_mode.get(ul_mode, _H.RIG_MODE_FM)
        vfo_main = int(_H.RIG_VFO_MAIN)
        vfo_sub = int(_H.RIG_VFO_SUB)
        func_tone = _H.RIG_FUNC_TONE
        enable = ctcss_hz > 0
        tone_deci = int(round(abs(ctcss_hz) * 10)) if enable else 0

        # TEMP_DATA_MODE_DIAG_LOG (GitHub Issue #16 — remove once root-caused):
        # confirm the requested mode strings resolved to the Hamlib constants
        # we expect (e.g. "-D" modes -> RIG_MODE_PKTUSB/PKTLSB) before any CI-V
        # is sent. _open_rig_with_retry() below already activates Hamlib's own
        # raw CI-V trace to hamlib_trace.log, so this app-level log line is
        # only for correlating that trace with which call it belongs to.
        logger.info(
            "RigDirect._apply_mode_and_ctcss_hamlib: DIAG requested dl_mode=%s(hamlib=%d) "
            "ul_mode=%s(hamlib=%d)",
            dl_mode,
            dl_hamlib,
            ul_mode,
            ul_hamlib,
        )

        def _make_rig() -> Any:
            r = _H.Rig(self._model_id)
            r.set_conf("rig_pathname", self._port)
            r.set_conf("serial_speed", str(self._baud_rate))
            if self._civ_addr:
                r.set_conf("civaddr", normalize_civ_addr(self._civ_addr))
            return r

        # Detect same-band from stored transponder frequencies.
        # IC-9100/9700 satmode requires different bands for Main/Sub; same-band
        # pairs must use normal split (VFO-A=RX, VFO-B=TX) instead.
        is_same_band = (
            self._transponder_dl_hz is not None
            and self._transponder_ul_hz is not None
            and self._freq_band(self._transponder_dl_hz) == self._freq_band(self._transponder_ul_hz)
        )

        rig = None
        rig2 = None
        with self._port_lock:
            try:
                # Delays throughout this sequence are deliberately more
                # generous than the ~0.1-0.3s that sufficed in Linux testing
                # -- see the equivalent comment in connect() for why. Each
                # write is also return-code-checked (raises RigControlError
                # on failure) so a rejected/timed-out command surfaces
                # instead of leaving part of the rig un-configured while
                # everything else silently proceeds as if it succeeded.
                if is_same_band:
                    # Same-band path (V/V or U/U): exit SAT mode, use normal split.
                    # CTCSS is intentionally skipped — same-band satellite transponders
                    # do not require uplink access tones.
                    vfo_a = int(_H.RIG_VFO_A)
                    vfo_b = int(_H.RIG_VFO_B)
                    vfo_curr = int(_H.RIG_VFO_CURR)
                    rig2 = _make_rig()
                    _open_rig_with_retry(rig2, "same-band: open()")
                    time.sleep(0.5)
                    rig2.set_func(_H.RIG_FUNC_SATMODE, 0)
                    _check_rig_ok(rig2, "same-band: set_func(SATMODE,0)")
                    time.sleep(0.6)  # wait for IC-9100 normal-mode memory restore
                    rig2.set_split_vfo(vfo_curr, 1, vfo_b)
                    _check_rig_ok(rig2, "same-band: set_split_vfo")
                    time.sleep(0.2)
                    # Write frequencies to anchor VFO-A/B band assignment
                    rig2.set_freq(vfo_a, int(self._transponder_dl_hz))  # type: ignore[arg-type]
                    _check_rig_ok(rig2, "same-band: set_freq(VFOA/DL)")
                    time.sleep(0.2)
                    rig2.set_freq(vfo_b, int(self._transponder_ul_hz))  # type: ignore[arg-type]
                    _check_rig_ok(rig2, "same-band: set_freq(VFOB/UL)")
                    time.sleep(0.2)
                    rig2.set_mode(dl_hamlib, 0, vfo_a)
                    _check_rig_ok(rig2, "same-band: set_mode(VFOA/DL)")
                    time.sleep(0.2)
                    rb_dl_mode, rb_dl_pb = rig2.get_mode(vfo_a)
                    logger.info(
                        "RigDirect._apply_mode_and_ctcss_hamlib: DIAG readback "
                        "VFOA/DL mode=%d(requested %d) pb=%d",
                        rb_dl_mode,
                        dl_hamlib,
                        rb_dl_pb,
                    )
                    rig2.set_mode(ul_hamlib, 0, vfo_b)
                    _check_rig_ok(rig2, "same-band: set_mode(VFOB/UL)")
                    time.sleep(0.2)
                    rb_ul_mode, rb_ul_pb = rig2.get_mode(vfo_b)
                    logger.info(
                        "RigDirect._apply_mode_and_ctcss_hamlib: DIAG readback "
                        "VFOB/UL mode=%d(requested %d) pb=%d",
                        rb_ul_mode,
                        ul_hamlib,
                        rb_ul_pb,
                    )
                    rig2.set_vfo(vfo_a)  # restore display to DL VFO
                    logger.info(
                        "RigDirect._apply_mode_and_ctcss_hamlib: same-band dl=%s ul=%s no-CTCSS OK",
                        dl_mode,
                        ul_mode,
                    )
                else:
                    # Cross-band path: SAT mode sequence.
                    # Step 1: open → set_func(SATMODE, 1) → close
                    rig = _make_rig()
                    _open_rig_with_retry(rig, "cross-band: open() [step 1]")
                    time.sleep(0.5)
                    rig.set_func(_H.RIG_FUNC_SATMODE, 1)
                    _check_rig_ok(rig, "cross-band: set_func(SATMODE,1)")
                    time.sleep(0.5)
                    rig.close()
                    rig = None
                    time.sleep(0.5)

                    # Step 2: second open() reads satmode=1 → cache->satmode=1
                    rig2 = _make_rig()
                    _open_rig_with_retry(rig2, "cross-band: open() [step 2]")
                    time.sleep(0.5)
                    # IC-9700: force cache->satmode=1 with extra set_func after open().
                    if self._model_id in _SATMODE_USE_VFO_SUB:
                        rig2.set_func(_H.RIG_FUNC_SATMODE, 1)
                        _check_rig_ok(rig2, "cross-band: IC-9700 extra set_func(SATMODE,1)")
                        time.sleep(0.2)
                        logger.info(
                            "RigDirect._apply_mode_and_ctcss_hamlib: IC-9700 extra set_func"
                        )

                    # Stage 1: write DL/UL frequencies to anchor IC-9100/9700 SAT mode
                    # Main/Sub band assignment BEFORE setting modes.
                    if self._transponder_dl_hz is not None:
                        vfo_ul_preset = (
                            int(_H.RIG_VFO_SUB)
                            if self._model_id in _SATMODE_USE_VFO_SUB
                            else int(_H.RIG_VFO_TX)
                        )
                        logger.info(
                            "RigDirect._apply_mode_and_ctcss_hamlib: freq preset DL=%.3fMHz",
                            self._transponder_dl_hz / 1e6,
                        )
                        rig2.set_freq(vfo_main, int(self._transponder_dl_hz))
                        _check_rig_ok(rig2, "cross-band: freq preset DL")
                        time.sleep(0.2)
                        if self._transponder_ul_hz is not None:
                            logger.info(
                                "RigDirect._apply_mode_and_ctcss_hamlib: freq preset UL=%.3fMHz",
                                self._transponder_ul_hz / 1e6,
                            )
                            rig2.set_freq(vfo_ul_preset, int(self._transponder_ul_hz))
                            _check_rig_ok(rig2, "cross-band: freq preset UL")
                            time.sleep(0.2)

                    # Mode: Main (DL) and Sub (UL)
                    rig2.set_mode(dl_hamlib, 0, vfo_main)
                    _check_rig_ok(rig2, "cross-band: set_mode(MAIN/DL)")
                    time.sleep(0.2)
                    rb_dl_mode, rb_dl_pb = rig2.get_mode(vfo_main)
                    logger.info(
                        "RigDirect._apply_mode_and_ctcss_hamlib: DIAG readback "
                        "MAIN/DL mode=%d(requested %d) pb=%d",
                        rb_dl_mode,
                        dl_hamlib,
                        rb_dl_pb,
                    )
                    rig2.set_mode(ul_hamlib, 0, vfo_sub)
                    _check_rig_ok(rig2, "cross-band: set_mode(SUB/UL)")
                    time.sleep(0.2)
                    rb_ul_mode, rb_ul_pb = rig2.get_mode(vfo_sub)
                    logger.info(
                        "RigDirect._apply_mode_and_ctcss_hamlib: DIAG readback "
                        "SUB/UL mode=%d(requested %d) pb=%d",
                        rb_ul_mode,
                        ul_hamlib,
                        rb_ul_pb,
                    )
                    # NOTE (GitHub Issue #16): the is_data_mode flag on Sub
                    # does not stick via Hamlib's set_mode() above (confirmed
                    # live -- readback shows base sideband correct, DATA flag
                    # not applied; root cause traced to icom_set_mode()
                    # forcing -RIG_ENAVAIL for its fast data-mode path
                    # whenever force_vfo_swap is set, which it always is for
                    # RIG_VFO_SUB on Main/Sub+A/B rigs). A raw-CI-V workaround
                    # via rig.send_raw() was tried here and reverted: it
                    # crashed the process live (stack smashing in Hamlib's
                    # Python SWIG binding -- the same class of risk this
                    # project's CLAUDE.md already flagged send_raw() for).
                    # Do not re-add a send_raw()-based fix without a safer
                    # mechanism confirmed not to crash on real hardware.
                    # CTCSS on Sub (TX/UL)
                    rig2.set_vfo(vfo_sub)
                    _check_rig_ok(rig2, "cross-band: set_vfo(SUB) for CTCSS")
                    time.sleep(0.2)
                    if enable:
                        # Only send an actual tone value when enabling one --
                        # sending tone_deci=0 (no real CTCSS tone is 0 Hz) to
                        # "disable" is rejected by the rig (Hamlib error -9,
                        # confirmed live against an IC-9700 selecting RS-44,
                        # which has no CTCSS tone at all). set_func(TONE, 0)
                        # below is what actually disables the encoder.
                        rig2.set_ctcss_tone(vfo_sub, tone_deci)
                        _check_rig_ok(rig2, "cross-band: set_ctcss_tone(SUB)")
                        time.sleep(0.2)
                    rig2.set_func(func_tone, 1 if enable else 0)
                    _check_rig_ok(rig2, "cross-band: set_func(TONE, SUB)")
                    time.sleep(0.2)
                    # Restore Main and clear any bleed-through
                    rig2.set_vfo(vfo_main)
                    _check_rig_ok(rig2, "cross-band: set_vfo(MAIN) restore")
                    time.sleep(0.1)
                    rig2.set_func(func_tone, 0)
                    _check_rig_ok(rig2, "cross-band: set_func(TONE off, MAIN)")
                    logger.info(
                        "RigDirect._apply_mode_and_ctcss_hamlib: dl=%s ul=%s ctcss=%.1fHz OK",
                        dl_mode,
                        ul_mode,
                        ctcss_hz,
                    )
                self._last_hamlib_error = None
                return True
            except Exception as exc:
                self._last_hamlib_error = str(exc)
                logger.error("RigDirect._apply_mode_and_ctcss_hamlib: %s", exc)
                return False
            finally:
                if rig is not None:
                    with contextlib.suppress(Exception):
                        rig.close()
                if rig2 is not None:
                    with contextlib.suppress(Exception):
                        rig2.close()

    def _apply_mode_and_ctcss_cat_ftx1(self, dl_mode: str, ul_mode: str, ctcss_hz: float) -> None:
        """Set mode and CTCSS on FTX-1F via raw CAT commands (no Hamlib calls).

        Uses MD commands (no VFO switching required) followed by existing
        CN/CT CTCSS commands.  All writes go through os.open() on the serial
        port so the rig does not need to be connected via Hamlib.

        Sequence:
          MD1{ul_code};   — SUB side (TX/UL) mode
          MD0{dl_code};   — MAIN side (RX/DL) mode
          CN10{tone:03d}; — CTCSS tone number on SUB (if tone > 0)
          CT11;           — CTCSS ENC ON on SUB (if tone > 0)
          CT10;           — CTCSS OFF on SUB (if tone <= 0)
        """
        ul_code = _FTX1_MODE_CODES.get(ul_mode, "4")
        dl_code = _FTX1_MODE_CODES.get(dl_mode, "4")

        commands: list[bytes] = [
            f"MD1{ul_code};".encode(),
            f"MD0{dl_code};".encode(),
        ]

        if ctcss_hz > 0:
            tone_number = CTCSS_TABLE.get(ctcss_hz)
            if tone_number is not None:
                commands.append(f"CN10{tone_number:03d};".encode())
                commands.append(b"CT11;")
            else:
                logger.warning(
                    "RigDirect.ftx1: %.1f Hz not in CTCSS_TABLE, skipping CTCSS", ctcss_hz
                )
        else:
            commands.append(b"CT10;")

        logger.info(
            "RigDirect: FTX-1 CAT mode+CTCSS dl=%s ul=%s ctcss=%.1f port=%s",
            dl_mode,
            ul_mode,
            ctcss_hz,
            self._port,
        )
        for raw in commands:
            try:
                fd = os.open(self._port, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
                try:
                    os.write(fd, raw)
                finally:
                    os.close(fd)
            except OSError as exc:
                logger.error("RigDirect.ftx1 write(%r): %s", raw, exc)

    def _apply_mode_and_ctcss_cat_ft991(self, dl_mode: str, ul_mode: str, ctcss_hz: float) -> None:
        """Set mode and CTCSS on FT-991/FT-991A via raw CAT commands (no Hamlib calls).

        FT-991/FT-991A MD command only targets Main VFO (P1=0 fixed).
        VFO-B (UL/TX) mode requires SV swap:
          SV;            — swap VFO-B to Main
          MD0{ul_code};  — set UL mode on (now Main = original VFO-B)
          SV;            — swap back, VFO-A is Main again
          MD0{dl_code};  — set DL mode on Main (VFO-A)

        CTCSS (TX-global, no SV swap needed):
          CN00{tone:03d}; — CTCSS tone index (P1=0 fixed, P2=0=CTCSS)
          CT02;           — CTCSS ENC ON
          CT00;           — CTCSS OFF (when ctcss_hz <= 0)

        Uses pyserial (not os.open) so that termios / baud rate are configured
        correctly.  Acquires _port_lock to prevent race with rig.open() in
        connect().
        """
        dl_code = _FT991_MODE_MAP.get(dl_mode, "4")
        ul_code = _FT991_MODE_MAP.get(ul_mode, "4")

        commands: list[bytes] = [
            b"SV;",
            f"MD0{ul_code};".encode(),
            b"SV;",
            f"MD0{dl_code};".encode(),
        ]

        if ctcss_hz > 0:
            tone_number = CTCSS_TABLE.get(ctcss_hz)
            if tone_number is not None:
                commands.append(f"CN00{tone_number:03d};".encode())
                commands.append(b"CT02;")
            else:
                logger.warning(
                    "RigDirect.ft991: %.1f Hz not in CTCSS_TABLE, skipping CTCSS", ctcss_hz
                )
        else:
            commands.append(b"CT00;")

        logger.info(
            "RigDirect: FT-991 CAT mode+CTCSS dl=%s ul=%s ctcss=%.1f port=%s",
            dl_mode,
            ul_mode,
            ctcss_hz,
            self._port,
        )
        try:
            import serial  # pyserial — optional dependency

            with self._port_lock, serial.Serial(self._port, self._baud_rate, timeout=2) as ser:
                for raw in commands:
                    ser.write(raw)
                    time.sleep(0.05)  # brief inter-command gap for FT-991 processing
        except Exception as exc:
            logger.error("RigDirect.ft991 CAT: %s", exc)

    def _resend_mode_ctcss_via_rig(self) -> None:
        """Stage 2: re-apply mode and CTCSS via self._rig after first UL write.

        Called from _set_vfo_frequencies_locked when _pending_mode_ctcss is True
        and the first DL+UL frequency pair has been written to the rig.
        At that point IC-9100/9700 SAT mode Main/Sub band assignment is locked
        to the new satellite, so modes/CTCSS land on the correct VFOs.
        Called with _rig_cmd_lock held; self._rig is guaranteed non-None here.
        """
        if self._rig is None:
            return
        try:
            import Hamlib as _H  # noqa: PLC0415

            hamlib_mode: dict[str, int] = _build_live_hamlib_mode_map(_H)
            dl_hamlib = hamlib_mode.get(self._current_dl_mode, _H.RIG_MODE_FM)
            ul_hamlib = hamlib_mode.get(self._current_ul_mode, _H.RIG_MODE_FM)
            vfo_main = int(_H.RIG_VFO_MAIN)
            vfo_sub = int(_H.RIG_VFO_SUB)
            func_tone = _H.RIG_FUNC_TONE
            enable = self._current_ctcss_hz > 0
            tone_deci = int(round(self._current_ctcss_hz * 10)) if enable else 0
            self._rig.set_mode(dl_hamlib, 0, vfo_main)
            _check_rig_ok(self._rig, "stage2: set_mode(MAIN/DL)")
            time.sleep(0.05)
            self._rig.set_mode(ul_hamlib, 0, vfo_sub)
            _check_rig_ok(self._rig, "stage2: set_mode(SUB/UL)")
            time.sleep(0.05)
            self._rig.set_vfo(vfo_sub)
            _check_rig_ok(self._rig, "stage2: set_vfo(SUB) for CTCSS")
            time.sleep(0.05)
            self._rig.set_ctcss_tone(vfo_sub, tone_deci)
            _check_rig_ok(self._rig, "stage2: set_ctcss_tone(SUB)")
            time.sleep(0.05)
            self._rig.set_func(func_tone, 1 if enable else 0)
            _check_rig_ok(self._rig, "stage2: set_func(TONE, SUB)")
            time.sleep(0.05)
            self._rig.set_vfo(vfo_main)
            _check_rig_ok(self._rig, "stage2: set_vfo(MAIN) restore")
            time.sleep(0.05)
            self._rig.set_func(func_tone, 0)
            _check_rig_ok(self._rig, "stage2: set_func(TONE off, MAIN)")
            logger.info(
                "RigDirect: Stage-2 mode/CTCSS resent dl=%s ul=%s ctcss=%.1fHz",
                self._current_dl_mode,
                self._current_ul_mode,
                self._current_ctcss_hz,
            )
        except Exception as exc:
            logger.warning("RigDirect._resend_mode_ctcss_via_rig: %s", exc)

    def _send_freq_preset_direct(self, dl_hz: float, ul_hz: float) -> None:
        """Briefly open the rig to write DL/UL frequencies at transponder selection.

        Mirrors NET-mode _send_freq_preset_independent() so the rig display
        shows the correct frequencies immediately, before the user presses Connect.
        Skipped when the rig is already connected (Doppler loop writes frequencies).
        Uses _port_lock so it cannot race with connect() / apply_transponder_state().

        FT-991/991A/991AM: use raw CAT via pyserial (FA/FB/FT3) — Hamlib
        set_split_vfo is unreliable for this model (ST command not supported).
        Other models: use Hamlib open/set_freq/close as before.
        """
        with self._port_lock:
            if self._rig is not None:
                return  # Doppler loop is running; let it handle frequencies
            try:
                if self._model_id in _FTX1_MODEL_IDS:
                    import serial as _serial

                    with _serial.Serial(self._port, self._baud_rate, timeout=1) as ser:
                        ser.write(f"FA{int(dl_hz):09d};".encode())
                        time.sleep(0.05)
                        ser.write(f"FB{int(ul_hz):09d};".encode())
                        time.sleep(0.05)
                        ser.write(b"FT1;")  # VFO-B TX = split ON (FTX-1F)
                elif self._model_id in _FT991_DIRECT_MODEL_IDS:
                    import serial as _serial

                    with _serial.Serial(self._port, self._baud_rate, timeout=1) as ser:
                        ser.write(f"FA{int(dl_hz):09d};".encode())
                        time.sleep(0.05)
                        ser.write(f"FB{int(ul_hz):09d};".encode())
                        time.sleep(0.05)
                        ser.write(b"FT3;")  # VFO-B TX = split ON (FT-991)
                elif self._model_id in _IC705_MODEL_IDS:
                    # IC-705: Hamlib's set_split_vfo() intermittently rejects
                    # this call (confirmed live — see _init_split() for the
                    # same fix). Send the raw CI-V split-ON frame via a
                    # separate pyserial connection first, then use Hamlib
                    # normally for the DL/UL frequency writes (those are
                    # reliable).
                    #
                    # Read back the rig's ACK before closing this session —
                    # see _init_split() for why an unread reply left in the
                    # kernel's tty input queue desyncs the Hamlib session
                    # opened right after.
                    import serial as _serial

                    civ = (
                        int(normalize_civ_addr(self._civ_addr), 16)
                        if self._civ_addr
                        else _IC705_DEFAULT_CIV_ADDR
                    )
                    with _serial.Serial(self._port, self._baud_rate, timeout=1) as ser:
                        ser.write(bytes([0xFE, 0xFE, civ, 0xE0, 0x0F, 0x01, 0xFD]))
                        ser.read(32)
                    time.sleep(0.1)

                    import Hamlib as _H

                    r = _H.Rig(self._model_id)
                    r.set_conf("rig_pathname", self._port)
                    r.set_conf("serial_speed", str(self._baud_rate))
                    r.open()
                    time.sleep(0.1)
                    r.set_freq(_H.RIG_VFO_A, int(dl_hz))
                    r.set_freq(_H.RIG_VFO_B, int(ul_hz))
                    r.close()
                else:
                    import Hamlib as _H

                    r = _H.Rig(self._model_id)
                    r.set_conf("rig_pathname", self._port)
                    r.set_conf("serial_speed", str(self._baud_rate))
                    r.open()
                    time.sleep(0.1)
                    r.set_split_vfo(_H.RIG_VFO_CURR, 1, _H.RIG_VFO_B)
                    r.set_freq(_H.RIG_VFO_A, int(dl_hz))
                    r.set_freq(_H.RIG_VFO_B, int(ul_hz))
                    r.close()
                logger.info(
                    "RigDirect: freq preset DL=%.3fMHz UL=%.3fMHz done",
                    dl_hz / 1e6,
                    ul_hz / 1e6,
                )
            except Exception as exc:
                logger.error("RigDirect: freq preset failed: %s", exc)

    def apply_transponder_state(self, dl_mode: str, ul_mode: str, ctcss_hz: float) -> None:
        """Apply mode and CTCSS atomically for Direct-mode rigs.

        Satmode rigs (IC-9100 / IC-9700 etc.):
          A single CI-V serial session handles SATMODE OFF→ON, DL/UL mode
          bytes, and CTCSS tone — no Hamlib call, no race condition.

        FTX-1F (model 1051):
          Raw CAT commands (MD / CN / CT) via os.open() — no VFO switching,
          no Hamlib call.  (The long freeze seen during development was caused
          by an incorrect baud rate setting, not a fundamental GIL issue.)

        FT-991 / FT-991A / FT-991AM (model 1035):
          Raw CAT via pyserial — SV swap for UL mode, CN/CT for CTCSS (no swap).

        Non-satmode rigs (other):
          Falls back to the base-class default (send_mode_only via Hamlib,
          then set_ctcss_tone via Hamlib).  These rigs are unaffected.
        """
        if self._satmode:
            self._current_dl_mode = dl_mode
            self._current_ul_mode = ul_mode
            self._current_ctcss_hz = ctcss_hz
            logger.info(
                "RigDirect: apply_transponder_state (satmode Hamlib) dl=%s ul=%s ctcss=%.1f",
                dl_mode,
                ul_mode,
                ctcss_hz,
            )
            if not self._apply_mode_and_ctcss_hamlib(dl_mode, ul_mode, ctcss_hz):
                detail = self._last_hamlib_error or "unknown error"
                raise RigControlError(f"Mode/CTCSS Error: {detail}")
            # Stage 2: after connect() + first Doppler UL write, re-confirm mode/CTCSS.
            # Only for cross-band — same-band uses normal split and _satmode_exit()
            # re-applies modes at Doppler startup; Stage-2 VFO_MAIN/SUB would be wrong.
            is_same_band_xpdr = (
                self._transponder_dl_hz is not None
                and self._transponder_ul_hz is not None
                and self._freq_band(self._transponder_dl_hz)
                == self._freq_band(self._transponder_ul_hz)
            )
            if not is_same_band_xpdr:
                self._pending_mode_ctcss = True
        elif self._model_id in _FTX1_MODEL_IDS:
            self._apply_mode_and_ctcss_cat_ftx1(dl_mode, ul_mode, ctcss_hz)
        elif self._model_id in _FT991_DIRECT_MODEL_IDS:
            self._apply_mode_and_ctcss_cat_ft991(dl_mode, ul_mode, ctcss_hz)
        else:
            super().apply_transponder_state(dl_mode, ul_mode, ctcss_hz)

    def send_ctcss_cat(
        self,
        tone_hz: float,
        cat_on_template: str,
        cat_off_template: str,
    ) -> None:
        """Send a custom CTCSS CAT command directly to the serial port.

        Writes each ';'-separated sub-command to the serial device using a
        direct file write (equivalent to printf '...' > /dev/FTX1CAT).
        Silently ignores errors (best-effort).
        """
        if tone_hz > 0 and cat_on_template:
            tone_number = CTCSS_TABLE.get(tone_hz)
            if tone_number is None:
                logger.warning("RigDirect.send_ctcss_cat: %.1f Hz not in CTCSS_TABLE", tone_hz)
                return
            template = cat_on_template.format(tone=tone_number)
        elif cat_off_template:
            template = cat_off_template
        else:
            return
        logger.info("RigDirect: send_ctcss_cat template=%r port=%s", template, self._port)
        for sub in template.split(";"):
            sub = sub.strip()
            if not sub:
                continue
            raw = (sub + ";").encode()
            try:
                fd = os.open(self._port, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
                try:
                    os.write(fd, raw)
                finally:
                    os.close(fd)
            except OSError as exc:
                logger.error("RigDirect.send_ctcss_cat write(%r): %s", raw, exc)

    def get_rig_info(self) -> RigInfo | None:
        """Return info about the connected rig."""
        if not self.is_connected:
            return None
        model_name = f"Model {self._model_id}"
        if self._hamlib is not None and self._rig is not None:
            with contextlib.suppress(Exception):
                model_name = self._rig.caps.model_name
        return RigInfo(
            model_id=self._model_id,
            model_name=model_name,
            port=self._port,
            baud_rate=self._baud_rate,
            state=self.state,
        )

    # -- Internal utilities --

    def _apply_ctcss_civ_via_send_raw(self, tone_hz: float) -> None:
        """Apply CTCSS via rig.send_raw() while Hamlib already holds the port.

        rig.send_raw(bytes) writes arbitrary bytes to the CI-V bus without
        going through Hamlib's command dispatching, so it works even when
        set_func(TONE) generates no CI-V for the IC-9100.
        """
        if self._rig is None:
            return
        civ = self._civ_addr_int()
        ctrl = 0xE0

        def frame(*payload: int) -> bytes:
            return bytes([0xFE, 0xFE, civ, ctrl, *payload, 0xFD])

        enable = tone_hz > 0
        tone_bcd = self._civ_bcd_tone(tone_hz) if enable else b"\x00\x00"
        tone_byte = 0x01 if enable else 0x00
        try:
            import time as _time

            self._rig.send_raw(frame(0x07, 0xD1))  # Select Sub
            _time.sleep(0.15)
            self._rig.send_raw(frame(0x1B, 0x00, *tone_bcd))  # Tone freq
            _time.sleep(0.15)
            self._rig.send_raw(frame(0x16, 0x42, tone_byte))  # TONE ON/OFF
            _time.sleep(0.15)
            self._rig.send_raw(frame(0x07, 0xD0))  # Select Main
            logger.info(
                "RigDirect: send_raw CTCSS %.1fHz enable=%s civ=0x%02X applied",
                tone_hz,
                enable,
                civ,
            )
        except Exception as exc:
            logger.error("RigDirect._apply_ctcss_civ_via_send_raw: %s", exc)

    def _satmode_exit(self) -> None:
        """Disable satmode and enable normal VFO-A/B split (same-band duplex).

        IC-9100 satmode always assigns Main and Sub to *different* bands, so
        same-band pairs (V/V FM, U/U) must use conventional split instead.
        """
        if self._rig is None or self._hamlib is None:
            return
        _H = self._hamlib
        if not hasattr(_H, "RIG_FUNC_SATMODE"):
            return
        rx_vfo = self._vfo_str_to_const("VFOA")
        tx_vfo = self._vfo_str_to_const("VFOB")
        logger.info("RigDirect: exiting satmode → normal VFO-A/B split (same-band)")
        try:
            self._rig.set_func(_H.RIG_FUNC_SATMODE, 0)
            time.sleep(0.4)  # wait for IC-9100 internal normal-mode memory restore
            self._rig.set_split_vfo(rx_vfo, 1, tx_vfo)
            # IC-9100 restores normal-mode memory on SAT exit (typically USB).
            # Re-apply the transponder's DL/UL modes explicitly.
            if self._current_dl_mode:
                self.set_mode(self._current_dl_mode, vfo="VFOA")
            if self._current_ul_mode:
                self.set_mode(self._current_ul_mode, vfo="VFOB")
        except Exception as exc:
            logger.warning("RigDirect: _satmode_exit failed — %s", exc)
        finally:
            self._satmode_active = False
            self._last_dl_hz = None
            self._last_dl_update_time = 0.0
            self._last_ul_hz = None
            self._last_ul_update_time = 0.0

    def _init_split(self) -> None:
        """Enable split/satmode. Called once at connect.

        Satmode rigs (IC-9100/IC-9700/IC-910H): SAT mode was entered via
        Hamlib set_func(SATMODE, 1) in connect() before the final rig.open().
        On the final open Hamlib reads satmode=1 and sets cache->satmode=1,
        which allows set_freq(VFO_TX) to write the UL frequency correctly.
        Generic rigs: conventional VFOA/VFOB split via set_split_vfo.
        """
        if self._rig is None:
            return
        try:
            if self._satmode:
                self._satmode_active = True
                logger.info("RigDirect: satmode active (entered via CI-V 16 5A before open)")
            elif self._model_id in _FTX1_MODEL_IDS:
                # FTX-1F: Hamlib set_split_vfo returns None and does not
                # reliably set VFO-B as TX.  Use raw CAT instead:
                #   FT0; = VFO-A TX (Main TX — used on app exit)
                #   FT1; = VFO-B TX (Sub TX — split ON)
                import os as _os

                _fd = _os.open(self._port, _os.O_WRONLY | _os.O_NOCTTY | _os.O_NONBLOCK)
                try:
                    _os.write(_fd, b"FT1;")
                finally:
                    _os.close(_fd)
                logger.info("RigDirect: split enabled via raw CAT FT1; (FTX-1)")
            elif self._model_id in _FT991_DIRECT_MODEL_IDS:
                # FT-991/991A: Hamlib set_split_vfo is unreliable.
                # The FT-991A does not implement the ST command (?; response).
                # Split is controlled via the FT command:
                #   FT2; = VFO-A TX (split OFF)
                #   FT3; = VFO-B TX (split ON)
                # We use os.open(O_NOCTTY|O_NONBLOCK) so Hamlib's termios
                # settings are not disturbed while it holds the port open.
                import os as _os

                _fd = _os.open(self._port, _os.O_WRONLY | _os.O_NOCTTY | _os.O_NONBLOCK)
                try:
                    _os.write(_fd, b"FT3;")
                finally:
                    _os.close(_fd)
                logger.info("RigDirect: split enabled via raw CAT FT3; (FT-991)")
            elif self._model_id in _IC705_MODEL_IDS:
                # IC-705: Hamlib's set_split_vfo() intermittently rejects
                # this call with "unsupported split" (confirmed live — same
                # flakiness already worked around on the exit-cleanup path
                # in main_window._release_rig_split_on_exit()).  Send the
                # raw CI-V split-ON frame directly instead, same os.open()
                # pattern as FTX-1F/FT-991 above (works alongside Hamlib
                # already holding the port).
                #
                # The write used to be fire-and-forget (no read of the
                # rig's ACK).  The IC-705 always replies to a CI-V command;
                # an unread reply byte sits in the kernel's tty input queue
                # (shared across every fd opened on this device node, not
                # per-fd) until *something* reads it — which ends up being
                # Hamlib's own self._rig session on its next unrelated
                # read(), permanently offsetting every subsequent
                # request/response pair for the rest of the connection
                # (confirmed 2026-07-07 via Hamlib debug trace: a VFO-select
                # ACK request came back with an unrelated stale frequency-
                # query response).  Read back the reply before closing so
                # nothing is left for Hamlib to trip over.
                import os as _os

                civ = (
                    int(normalize_civ_addr(self._civ_addr), 16)
                    if self._civ_addr
                    else _IC705_DEFAULT_CIV_ADDR
                )
                frame = bytes([0xFE, 0xFE, civ, 0xE0, 0x0F, 0x01, 0xFD])
                _fd = _os.open(self._port, _os.O_RDWR | _os.O_NOCTTY | _os.O_NONBLOCK)
                try:
                    _os.write(_fd, frame)
                    time.sleep(0.1)
                    with contextlib.suppress(OSError):
                        _os.read(_fd, 32)
                finally:
                    _os.close(_fd)
                logger.info("RigDirect: split enabled via raw CI-V 0F 01 (IC-705)")
            else:
                import Hamlib as _H

                # Passing an explicit RX vfo (RIG_VFO_A) here caused the
                # Icom CI-V backend (confirmed on IC-705) to invert which
                # VFO becomes TX.  RIG_VFO_CURR avoids the inversion — it's
                # what _send_freq_preset_direct() already uses successfully.
                tx_vfo = self._vfo_str_to_const("VFOB")
                ret = self._rig.set_split_vfo(_H.RIG_VFO_CURR, 1, tx_vfo)
                logger.info("RigDirect: set_split_vfo(CURR,1,VFOB) ret=%d", ret)
        except Exception as exc:
            logger.warning("RigDirect: _init_split failed — %s", exc)

    def satmode_warmup(self) -> None:
        """Pre-initialize satmode hardware before the user presses Connect.

        Opens the rig, sends set_func(RIG_FUNC_SATMODE, 1) via Hamlib (which
        automatically uses the correct CI-V per model: 16 5A for IC-9100/9700,
        1A 07 for IC-910H), then closes.  Called from a background thread at
        app startup so the delay is invisible to the user.
        """
        if not self._satmode:
            return
        try:
            import Hamlib as _H
        except ImportError:
            logger.warning("RigDirect: satmode_warmup — Hamlib not available")
            return
        if not hasattr(_H, "RIG_FUNC_SATMODE"):
            return
        try:
            rig = _H.Rig(self._model_id)
            rig.set_conf("rig_pathname", self._port)
            rig.set_conf("serial_speed", str(self._baud_rate))
            if self._civ_addr:
                rig.set_conf("civaddr", normalize_civ_addr(self._civ_addr))
            _open_rig_with_retry(rig, "RigDirect satmode_warmup: open()")
            time.sleep(0.3)
            rig.set_func(_H.RIG_FUNC_SATMODE, 1)
            time.sleep(0.1)
            rig.close()
            logger.info("RigDirect: satmode warmup complete (Hamlib set_func SATMODE)")
        except Exception as exc:
            logger.warning("RigDirect: satmode warmup failed — %s", exc)

    def _vfo_str_to_const(self, vfo: str) -> int:
        """Convert a VFO string to the corresponding Hamlib constant (or 0 in mock mode)."""
        if self._hamlib is None:
            return 0
        vfo_map = {
            "VFOA": self._hamlib.RIG_VFO_A,
            "VFOB": self._hamlib.RIG_VFO_B,
            "Main": self._hamlib.RIG_VFO_MAIN,
            "Sub": self._hamlib.RIG_VFO_SUB,
            "TX": self._hamlib.RIG_VFO_TX,
        }
        return int(vfo_map.get(vfo, self._hamlib.RIG_VFO_CURR))


# ---------------------------------------------------------------------------
# HamlibNetController (rigctld TCP connection)
# ---------------------------------------------------------------------------

# FT-991/FT-991A CAT mode codes for the MD command (e.g. MD02; = USB on VFO-A).
_FT991_MODE_MAP: dict[str, str] = {
    "LSB": "1",
    "USB": "2",
    "CW": "3",
    "FM": "4",
    "AM": "5",
    "CW-R": "7",
    "LSB-D": "8",  # DATA-LSB (data mode, e.g. FT4 calling freqs)
    "FM-N": "B",
    "USB-D": "C",  # DATA-USB
}


class HamlibNetController(RigController):
    """
    Transceiver controller that connects to rigctld over TCP.

    Compatible with GPredict NET Control mode — works with any existing
    rigctld setup. Uses the rigctld newline-delimited text protocol.
    """

    _TIMEOUT = 10.0  # seconds — allows for slow CAT backends such as FTX-1

    def __init__(
        self,
        host: str = "localhost",
        port: int = 4532,
        radio_type: str = "full_duplex",
        ctcss_method: str = "hamlib",
        ctcss_civ_addr: str = "",
        is_satmode_rig: bool = False,
    ) -> None:
        """
        Args:
            host:           Host where rigctld is running
            port:           rigctld port number (default 4532)
            radio_type:     "full_duplex"=send both F and I (default) /
                            "rx_only"=F only / "tx_only"=I only
            ctcss_method:   CTCSS method key ("hamlib", "ftx1", "ft991", "custom_cat").
            ctcss_civ_addr: Unused; kept for backward compatibility only.
            is_satmode_rig: True when the rig is an Icom satmode rig (IC-9100/9700/910H/821H).
                            Controls satmode split init and same-band detection.
        """
        super().__init__()
        self._host = host
        self._port = port
        self._radio_type = radio_type
        self._ctcss_method = ctcss_method
        self._ctcss_civ_addr = ctcss_civ_addr.strip()
        self._is_satmode_rig = is_satmode_rig
        self._sock: socket.socket | None = None
        self._vfo_mode: bool = False
        self._cmd_lock = threading.Lock()  # serialise send+recv to prevent response misalignment
        self._satmode: bool = False
        self._is_same_band: bool = False  # True when DL and UL are on the same band (V/V or U/U)
        self._current_dl_mode: str = ""  # updated by set_current_modes(); used for UL throttle
        self._current_ul_mode: str = (
            ""  # updated by set_current_modes() and apply_transponder_state
        )
        self._last_dl_hz: float | None = None  # None = just connected; forces the first F/I send
        self._last_ul_hz: float | None = None
        self._last_ul_update_time: float = 0.0  # monotonic; used for same-band UL throttle
        self._pending_ctcss_hz: float | None = None  # re-applied after connect() on satmode rigs
        # Transponder DL/UL frequencies stored at selection time for Stage-1 freq
        # pre-write in _send_freq_preset_independent (IC-9100/9700 SAT mode anchor).
        self._transponder_dl_hz: float | None = None
        self._transponder_ul_hz: float | None = None
        # Stage-2 flag: after first I (UL) write post-connect, re-send mode/CTCSS
        # with the connection's send_mode_only and _apply_ctcss_civ_direct.
        self._pending_mode_net: bool = False

    # -- Connection management --

    @property
    def is_satmode(self) -> bool:
        """True when the rig uses satmode.

        Returns True if the model name was detected at connect time, or if the
        "Icom SAT mode rig" checkbox was checked in Rig Settings — reliable even
        when rigctld model-name detection fails.
        """
        return self._satmode or self._is_satmode_rig

    def set_transponder_freqs(self, dl_hz: float, ul_hz: float) -> None:
        """Update same-band flag from transponder DL/UL frequencies.

        Called by the UI when a transponder is selected so that _init_vfo()
        and send_mode_only() know whether to use satmode (S 1 Main) or normal
        split (S 1 VFOB) for same-band transponders like ISS APRS (V/V).
        """
        self._is_same_band = self._freq_band(dl_hz) == self._freq_band(ul_hz)
        self._transponder_dl_hz = dl_hz
        self._transponder_ul_hz = ul_hz
        logger.info(
            "RigNet: transponder freqs dl=%.3fMHz ul=%.3fMHz same_band=%s",
            dl_hz / 1e6,
            ul_hz / 1e6,
            self._is_same_band,
        )

    def set_current_modes(self, dl_mode: str, ul_mode: str) -> None:
        """Store current DL/UL modes so same-band UL throttle can pick the right threshold."""
        self._current_dl_mode = dl_mode
        self._current_ul_mode = ul_mode

    @property
    def is_connected(self) -> bool:
        """True only when connected and the socket is valid."""
        with self._lock:
            return self._state == RigState.CONNECTED and self._sock is not None

    def connect(self) -> bool:
        """Establish a TCP connection to rigctld."""
        with self._lock:
            if self._state == RigState.CONNECTED:
                return True
            self._state = RigState.CONNECTING

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self._TIMEOUT)
            sock.connect((self._host, self._port))
            self._sock = sock
            with self._lock:
                self._state = RigState.CONNECTED
            # Reset frequency state so reconnection does not inherit the previous session.
            # Without this, _last_dl_hz is not None → the initial f-check is sent →
            # CAT delay after S 1 Main causes a timeout → immediate disconnect loop.
            self._last_dl_hz = None
            self._last_ul_hz = None
            self._last_ul_update_time = 0.0
            logger.info("RigNet: connected to %s:%d", self._host, self._port)
            # _ and \chk_vfo are optional info-query commands.
            # Sending them with a 2 s timeout over a raw socket leaves stale data in the
            # receive buffer on slow backends (e.g. FTX-1), causing subsequent _cmd() calls
            # to read the wrong response (command/response misalignment).
            # Only S 1 Main is sent during the connection sequence.
            self._init_vfo()
            # If _init_vfo()'s _cmd() raises OSError (including timeout) and closes the
            # socket, treat the connection as failed and transition to ERROR.
            if self._sock is None:
                with self._lock:
                    self._state = RigState.ERROR
                logger.error("RigNet: S 1 Main timed out or failed — aborting connect")
                return False
            # Re-apply CTCSS after SAT mode is established for satmode rigs (IC-9700 etc.).
            # Transponder selection sends CTCSS before connect(), at which point the rig
            # may not yet be in SAT mode — IC-9700 stores Normal-mode and SAT-mode CTCSS
            # separately, so the earlier write goes to Normal mode.  Resending here ensures
            # the tone lands in SAT mode after _init_vfo() (S 1 Main) has been sent.
            if self.is_satmode and self._pending_ctcss_hz is not None:
                logger.info(
                    "RigNet: re-applying CTCSS %.1f Hz after connect (satmode)",
                    self._pending_ctcss_hz,
                )
                self._apply_ctcss_civ_direct(self._pending_ctcss_hz)
            return True
        except OSError as exc:
            with self._lock:
                self._state = RigState.ERROR
            logger.error("RigNet: connect failed — %s", exc)
            return False

    def disconnect(self) -> None:
        """Disconnect the TCP connection.

        Closes the socket under _cmd_lock so this cannot race with an
        in-flight _cmd_raw() call from the background Doppler-update thread
        (which would otherwise raise "Bad file descriptor" there).
        """
        with self._lock:
            if self._state == RigState.DISCONNECTED:
                return
        with self._cmd_lock:
            try:
                if self._sock:
                    self._sock.close()
            except OSError:
                pass
            finally:
                self._sock = None
        self._last_dl_hz = None
        self._last_ul_hz = None
        self._last_ul_update_time = 0.0
        with self._lock:
            self._state = RigState.DISCONNECTED

    # -- Low-level communication --

    def _cmd_raw(self, command: str) -> str:
        """Send a command and return the response. Caller MUST hold _cmd_lock.

        Set commands (uppercase, e.g. "F", "I", "S") always reply with a
        trailing "RPRT <code>" line, so we read until that appears.

        Query commands (lowercase, e.g. "f", "i", "m") do NOT send an RPRT
        line on success -- only the raw value terminated by a newline (RPRT
        only appears on the query's *error* path). Waiting for RPRT on a
        successful query blocks until the socket timeout, since it never
        arrives. Confirmed live (2026-07-15, FTX-1F via rigctld) that this
        was the actual cause of "get_freq never works on this rig", not a
        genuine Hamlib/backend limitation as previously documented here.
        For query commands we instead stop as soon as a complete line
        (ending in "\\n") has been read.
        """
        if self._sock is None:
            return ""
        is_query = command[:1].islower()
        try:
            self._sock.sendall((command + "\n").encode())
            data = b""
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if is_query:
                    if data.endswith(b"\n"):
                        break
                elif b"RPRT" in data:
                    break
            return data.decode(errors="replace").strip()
        except OSError as exc:
            logger.error("RigNet._cmd(%r): %s", command, exc)
            with contextlib.suppress(OSError):
                if self._sock:
                    self._sock.close()
            self._sock = None
            with self._lock:
                self._state = RigState.DISCONNECTED
            return ""

    def _cmd(self, command: str) -> str:
        """Send a command to rigctld and return the response (thread-safe)."""
        with self._cmd_lock:
            return self._cmd_raw(command)

    def _init_vfo(self) -> None:
        """Enable split (called once at connect time).

        Satmode rigs (IC-9100 etc.) + same-band: S 1 VFOB → normal split
          (VFOA=RX, VFOB=TX).  Satmode rigs require this instead of S 1 Main
          because S 1 Main activates hardware satmode, which requires different
          bands on Main and Sub.
        Cross-band satmode, and FTX-1F/FT-991A (ctcss_method): S 1 Main.
          FTX-1F/FT-991A's rigctld backend forces Sub=TX regardless of the
          VFO argument, so S 1 VFOB would produce undefined behaviour — this
          was confirmed working specifically for those two models.
        Other non-satmode rigs (e.g. IC-705): S 1 VFOB.  These have no true
          Main/Sub VFO concept, so sending the literal "Main" as the tx_vfo
          argument gets misparsed by that rig's Hamlib backend and inverts
          which VFO becomes RX/TX (confirmed live on IC-705 — downlink
          landed on VFO-B instead of VFO-A). Plain VFOA/VFOB split (the same
          command already used for the satmode same-band fallback above)
          works correctly instead.
        Sent through _cmd() so _cmd_lock serialises it and prevents buffer
        residue from an independent recv loop on the raw socket.
        """
        is_satmode_rig = self._satmode or self._is_satmode_rig
        is_yaesu_cat = self._ctcss_method in ("ftx1", "ft991")
        if (is_satmode_rig and self._is_same_band) or (not is_satmode_rig and not is_yaesu_cat):
            resp = self._cmd("S 1 VFOB")
            logger.info("RigNet: VFOA/VFOB split init (S 1 VFOB)")
        else:
            resp = self._cmd("S 1 Main")
        if "RPRT 0" not in resp:
            logger.warning("RigNet: split setup returned %r", resp)

        if is_yaesu_cat:
            # rigctld shares a single RIG object across every TCP client
            # (tests/rigctld.c: static RIG *my_rig), so a past client on this
            # same port (GPredict itself, per rig_set_uplink()'s own doc
            # comment "For GPredict to avoid reading frequency on uplink
            # VFO") may have sent "\uplink 1"/"\uplink 2". Hamlib's
            # rig_get_freq() then returns a frozen cached value for the
            # ignored VFO indefinitely -- not on any timeout -- until reset
            # (src/rig.c). This is what caused the Lock dial-feedback
            # feature's live_dl read to freeze for arbitrary, varying
            # durations (confirmed 2026-07-20). Reset unconditionally on
            # every connect; harmless if it was already 0.
            uplink_resp = self._cmd(r"\uplink 0")
            if "RPRT 0" not in uplink_resp:
                logger.warning("RigNet: \\uplink 0 reset returned %r", uplink_resp)

    # -- Internal utilities --

    def _detect_vfo_mode(self) -> bool:
        r"""Send \chk_vfo to detect the rigctld VFO mode.

        Operates on the raw socket directly so that a timeout or unsupported
        command does not break the connection — returns False in that case.

        rigctld response format:
          vfo_mode=on  → "1\nRPRT 0\n"
          vfo_mode=off → "0\nRPRT 0\n"
          unsupported  → "RPRT -1\n"
          timeout      → OSError (socket.timeout)
        """
        if self._sock is None:
            return False
        prev_timeout = self._sock.gettimeout()
        try:
            self._sock.settimeout(2.0)
            self._sock.sendall(b"\\chk_vfo\n")
            data = b""
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"RPRT" in data:
                    break
            resp = data.decode(errors="replace").strip()
            lines = resp.splitlines()
            return bool(lines and lines[0].strip() == "1")
        except OSError as exc:
            logger.warning("RigNet: \\chk_vfo failed (vfo_mode=False assumed): %s", exc)
            return False
        finally:
            with contextlib.suppress(OSError):
                if self._sock is not None:
                    self._sock.settimeout(prev_timeout)

    @staticmethod
    def _normalize_vfo(vfo: str) -> str:
        """Normalise a VFO string to the form accepted by rigctld."""
        _map = {"VFOA": "VFOA", "VFOB": "VFOB", "Main": "Main", "Sub": "Sub"}
        return _map.get(vfo, vfo)

    # -- Frequency and mode --

    def _set_one_vfo(self, vfo: str, freq_hz: float) -> None:
        """Internal helper to set a single VFO frequency. Raises RigControlError on failure."""
        norm_vfo = self._normalize_vfo(vfo)
        if self._vfo_mode:
            resp = self._cmd(f"\\set_freq {norm_vfo} {int(freq_hz)}")
        else:
            vfo_resp = self._cmd(f"V {norm_vfo}")
            if "RPRT 0" not in vfo_resp:
                raise RigControlError(f"set_vfo({norm_vfo!r}) failed: {vfo_resp!r}")
            resp = self._cmd(f"F {int(freq_hz)}")
        if "RPRT 0" not in resp:
            raise RigControlError(f"set_frequency({freq_hz!r}, {norm_vfo!r}) failed: {resp!r}")
        with self._lock:
            self._freq_state.freq_hz = freq_hz

    def set_frequency(self, freq_hz: float, vfo: str = "VFOA") -> bool:
        """Set the frequency in Hz.

        Returns False when not connected.
        Raises RigControlError when the command fails while connected.
        No split command is sent (avoids split issues on FTX-1 and similar rigs).
        """
        if not self.is_connected:
            return False
        self._set_one_vfo(vfo, freq_hz)
        return True

    def set_vfo_frequencies(
        self,
        vfoa_hz: float | None,
        vfob_hz: float | None,
    ) -> bool:
        """Set RX/TX frequencies in the per-second tracking loop.

        Never sends f/i (get_freq/get_split_freq) commands itself -- this
        method remains write-only. Those reads do happen elsewhere now
        (get_frequency()/get_split_frequency(), called from
        MainWindow._rig_send() for Lock dial feedback) and work reliably.
        The original justification here ("f can take more than 10s and
        trigger a timeout") was a client-side bug in _cmd_raw(), not a
        genuine FTX-1F/backend limitation: it unconditionally waited for
        an "RPRT" line, which query commands never send on success (only
        on their error path) -- so a successful "f" blocked until the
        socket timeout every time. Fixed 2026-07-15; see _cmd_raw()'s
        docstring. This method itself is unchanged and still deliberately
        write-only, simply because nothing here needs to read anything.

        Write-only protocol:
          [RX cycle]
            F {dl_hz}  — write to Sub (RX/downlink)
                         only when changed by 1 Hz or more, or on the first call
                         (_last_dl_hz is None).
          [TX cycle]
            After the RX cycle, is_connected is checked; TX is skipped if disconnected.
            I {ul_hz}  — write to Main (TX/uplink)
                         only when changed by 1 Hz or more, or on the first call
                         (_last_ul_hz is None).

        connect() calls _init_vfo() which sends S 1 Sub (split ON, TX VFO=Sub):
          F → Main (RX/downlink)
          I → Sub (TX/uplink)
        The TX cycle is skipped when vfob_hz is None.
        """
        if not self.is_connected:
            return False

        # Skip Doppler updates during CAT PTT TX window (~0.8 s) to avoid
        # changing frequency while the rig is transmitting.
        if self._ptt_active:
            return True

        send_rx = self._radio_type != "tx_only"
        send_tx = self._radio_type != "rx_only"

        with self._cmd_lock:
            # RX cycle
            if send_rx and vfoa_hz is not None:
                last_dl = self._last_dl_hz
                if last_dl is None or abs(vfoa_hz - last_dl) >= 1.0:
                    logger.info("RigNet: sending F %d", int(vfoa_hz))
                    resp = self._cmd_raw(f"F {int(vfoa_hz)}")
                    if "RPRT 0" not in resp:
                        raise RigControlError(f"set RX freq failed: {resp!r}")
                    with self._lock:
                        self._freq_state.freq_hz = vfoa_hz
                    self._last_dl_hz = vfoa_hz

            # Skip TX and mode if F caused an OSError and disconnected
            if not self.is_connected:
                return True

            # TX cycle
            if send_tx and vfob_hz is not None:
                last_ul = self._last_ul_hz
                now = time.monotonic()
                elapsed = now - self._last_ul_update_time
                is_satmode_rig = self._satmode or self._is_satmode_rig
                if is_satmode_rig and self._is_same_band:
                    # Satmode rigs (IC-9100 etc.) same-band: throttle I command to
                    # suppress display flicker caused by rapid VFOA↔VFOB switching.
                    # FM capture range (±5 kHz) exceeds ISS max Doppler (±3.5 kHz),
                    # so coarse updates are sufficient.
                    # Non-satmode rigs (FTX-1F, FT-991A) do not flicker and always
                    # use the standard 1 Hz threshold below.
                    is_fm = self._current_dl_mode in ("FM", "AFSK", "DIGITALVOICE")
                    ul_thresh = 2000.0 if is_fm else 20.0
                    ul_max_s = 60.0 if is_fm else 15.0
                    send_ul = (
                        last_ul is None
                        or abs(vfob_hz - last_ul) >= ul_thresh
                        or elapsed >= ul_max_s
                    )
                else:
                    send_ul = last_ul is None or abs(vfob_hz - last_ul) >= 1.0
                if send_ul:
                    was_first_ul = last_ul is None
                    logger.info("RigNet: sending I %d", int(vfob_hz))
                    resp = self._cmd_raw(f"I {int(vfob_hz)}")
                    if "RPRT 0" not in resp:
                        raise RigControlError(f"set TX freq failed: {resp!r}")
                    self._last_ul_hz = vfob_hz
                    self._last_ul_update_time = now
                    if was_first_ul and (
                        self._pending_mode_net or self._pending_ctcss_hz is not None
                    ):
                        # Stage 2: SAT mode band assignment is now locked by the
                        # first live frequency write — re-send modes and CTCSS.
                        # send_mode_only and _apply_ctcss_civ_direct use independent
                        # sockets and do not acquire _cmd_lock, so this is safe.
                        _dl = self._current_dl_mode
                        _ul = self._current_ul_mode
                        _ctcss = self._pending_ctcss_hz
                        self._pending_mode_net = False
                        self._pending_ctcss_hz = None
                        if _dl:
                            self.send_mode_only(_dl, _ul)
                        if _ctcss is not None and _ctcss > 0.0:
                            self._apply_ctcss_civ_direct(_ctcss)
                        logger.info(
                            "RigNet: Stage-2 mode/CTCSS resent dl=%s ul=%s ctcss=%.1fHz",
                            _dl,
                            _ul,
                            _ctcss or 0.0,
                        )

        return True

    def get_frequency(self, vfo: str = "VFOA") -> float:
        resp = self._cmd("f")
        try:
            return float(resp.splitlines()[0])
        except (ValueError, IndexError):
            return -1.0

    def get_split_frequency(self) -> float:
        """Read back the split (TX/UL) frequency via rigctld's "i" command.

        Mirrors the write side's "I" (set_split_freq) exactly -- like "I",
        this does not depend on rigctld's "current VFO" tracking, and is
        reliable regardless of read/write ordering (confirmed live,
        2026-07-15, FTX-1F: unlike bare "f", "i" never returned a stale or
        wrong value across 10+ consecutive cycles including immediately
        after a fresh connection's first "I" write).
        """
        resp = self._cmd("i")
        try:
            return float(resp.splitlines()[0])
        except (ValueError, IndexError):
            return -1.0

    def set_mode(self, mode: str, passband_hz: int = 0, vfo: str = "VFOA") -> bool:
        # rigctld M command format: "M <mode> <passband>"
        hamlib_mode_name = _SATNOGS_TO_RIGCTLD_MODE.get(mode, "FM")
        resp = self._cmd(f"M {hamlib_mode_name} {passband_hz}")
        ok = "RPRT 0" in resp
        if ok:
            with self._lock:
                self._freq_state.mode = mode
        return ok

    def get_mode(self, vfo: str = "VFOA") -> str:
        resp = self._cmd("m")
        lines = resp.splitlines()
        if lines:
            rigctld_mode = lines[0].strip()
            return _RIGCTLD_MODE_TO_SATNOGS.get(rigctld_mode, "FM")
        return "FM"

    def set_ctcss_tone(self, tone_hz: float) -> bool:
        """Set CTCSS tone on VFO-B (the UL/TX vfo) via rigctld's "C" command.

        "L CTCSS_TONE {value}" (the LEVEL-set syntax) was used here
        previously, but CTCSS_TONE is not a rigctld LEVEL — it has its own
        command letter. rigctld rejects "L CTCSS_TONE" with RPRT -11
        (ENAVAIL), so this silently never worked for generic (non-CAT-
        template) rigs; confirmed live against an IC-705 via rigctld, where
        "C {value}" succeeds (RPRT 0) and reads back correctly.

        "C" applies to whichever VFO is currently selected — confirmed live
        that CTCSS tone is stored independently per VFO on the IC-705 (VFOA
        and VFOB can hold different tones). send_mode_only() leaves VFOA
        (downlink) selected, so writing the tone without first switching to
        VFOB landed it on the wrong (RX) side — it "succeeded" (RPRT 0) but
        had no effect on the satellite uplink. Select VFOB first, then
        restore VFOA so the rig's display doesn't change.

        "C" only sets the tone *frequency* — confirmed live that it does
        NOT enable the encoder: "u TONE" read back 0 both before and after
        a successful "C 670". The encoder is a separate rigctld func ("U
        TONE 1"/"U TONE 0"), same func/frequency split already known from
        the Direct-mode satmode path. Earlier manual tests only appeared to
        work because TONE happened to already be enabled from prior
        Direct-mode testing. A tone_hz <= 0 skips "C" (rigctld rejects tone
        0 with RPRT -9) and only disables the encoder.

        When not yet connected (e.g. transponder selected before Connect is
        pressed), self._cmd() requires the persistent self._sock and would
        silently no-op. Open a short-lived independent socket instead,
        mirroring _send_freq_preset_independent() / send_mode_only(), so
        CTCSS is applied at transponder-selection time like DL/UL and mode
        already are, instead of requiring Connect first.
        """
        enable = tone_hz > 0
        tone_int = int(round(tone_hz * 10))
        if self._sock is not None:
            self._cmd("V VFOB")
            resp = self._cmd(f"C {tone_int}") if enable else "RPRT 0"
            self._cmd(f"U TONE {1 if enable else 0}")
            self._cmd("V VFOA")
            return "RPRT 0" in resp
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self._TIMEOUT)
            sock.connect((self._host, self._port))
            sock.settimeout(2.0)

            def _send_recv(cmd: str) -> str:
                sock.sendall((cmd + "\n").encode())
                buf = b""
                with contextlib.suppress(OSError):
                    while b"RPRT" not in buf:
                        chunk = sock.recv(256)
                        if not chunk:
                            break
                        buf += chunk
                return buf.decode(errors="replace").strip()

            _send_recv("V VFOB")
            resp = _send_recv(f"C {tone_int}") if enable else "RPRT 0"
            _send_recv(f"U TONE {1 if enable else 0}")
            _send_recv("V VFOA")
            sock.close()
            logger.info("RigNet: CTCSS preset %.1fHz via independent socket -> %r", tone_hz, resp)
            return "RPRT 0" in resp
        except Exception as exc:
            logger.error("RigNet: set_ctcss_tone (independent socket): %s", exc)
            return False

    def set_dcs_code(self, code: int) -> bool:
        resp = self._cmd(f"L DCS_CODE {code}")
        return "RPRT 0" in resp

    def set_vfo(self, vfo: str) -> bool:
        resp = self._cmd(f"V {vfo}")
        return "RPRT 0" in resp

    def set_ptt(self, enabled: bool) -> bool:
        """Key (T 1) or un-key (T 0) via rigctld CAT PTT command."""
        super().set_ptt(enabled)  # updates _ptt_active
        if not self.is_connected:
            return False
        resp = self._cmd(f"T {'1' if enabled else '0'}")
        return "RPRT 0" in resp

    def _apply_ctcss_civ_direct(self, tone_hz: float) -> None:
        """Set CTCSS on Icom Sub band (TX/UL) via rigctld commands.

        Sends rigctld commands over an independent TCP socket (same pattern
        as send_mode_only).  No pyserial required — works on Linux, macOS,
        and Windows regardless of port locking.

        "L CTCSS_TONE {value}" (the LEVEL-set syntax) was used here
        previously, but CTCSS_TONE is not a rigctld LEVEL — it has its own
        command letter "C". rigctld rejects "L CTCSS_TONE" with RPRT -11
        (ENAVAIL), so the tone frequency was never actually written; only
        "U TONE" (the encoder on/off func, a separate command) succeeded,
        which is why the TONE indicator lit up while the tone itself stayed
        at whatever value was previously set on the rig. Confirmed live on
        an IC-9100 (2026-07-15) — same failure mode as the IC-705 fix in
        set_ctcss_tone() above. A tone_hz <= 0 skips "C" (rigctld rejects
        tone 0 with RPRT -9) and only disables the encoder.

        Sequence (via rigctld extended commands):
          V Sub          — select Sub VFO
          C <deci_hz>     — set tone frequency (deci-Hz integer; skipped when disabling)
          U TONE 1        — enable CTCSS encoder (0 to disable)
          V Main          — restore Main VFO
          U TONE 0        — clear CTCSS on Main (prevent bleed-through)
        """
        enable = tone_hz > 0
        tone_deci = int(round(abs(tone_hz) * 10)) if enable else 0
        logger.info(
            "RigNet._apply_ctcss_civ_direct: %.1f Hz enable=%s via rigctld", tone_hz, enable
        )
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self._TIMEOUT)
            sock.connect((self._host, self._port))
            sock.settimeout(2.0)

            def _cmd_drain(cmd: str) -> None:
                sock.sendall((cmd + "\n").encode())
                buf = b""
                with contextlib.suppress(OSError):
                    while b"RPRT" not in buf:
                        chunk = sock.recv(256)
                        if not chunk:
                            break
                        buf += chunk

            _cmd_drain("V Sub")
            if enable:
                _cmd_drain(f"C {tone_deci}")
            _cmd_drain(f"U TONE {'1' if enable else '0'}")
            _cmd_drain("V Main")
            _cmd_drain("U TONE 0")
            sock.close()
            logger.info("RigNet._apply_ctcss_civ_direct: done (%.1f Hz enable=%s)", tone_hz, enable)
        except Exception as exc:
            logger.error("RigNet._apply_ctcss_civ_direct: %s", exc)

    def send_ctcss_cat(
        self,
        tone_hz: float,
        cat_on_template: str,
        cat_off_template: str,
    ) -> None:
        """Send a custom CTCSS CAT command via a fresh TCP connection to rigctld.

        Opens an independent socket (same pattern as send_mode_only()) so this
        works regardless of the main connection state — _send_mode_only_to_rig()
        disconnects the main socket before calling send_mode_only(), which would
        leave self._sock=None and silently discard commands sent via _cmd().

        Each ';'-separated sub-command is wrapped as 'w <part>;' and forwarded
        verbatim to the rig's serial port by rigctld.
        """
        if tone_hz > 0 and cat_on_template:
            tone_number = CTCSS_TABLE.get(tone_hz)
            if tone_number is None:
                logger.warning("RigNet.send_ctcss_cat: %.1f Hz not in CTCSS_TABLE", tone_hz)
                return
            template = cat_on_template.format(tone=tone_number)
        elif cat_off_template:
            template = cat_off_template
        else:
            return
        parts = [p.strip() for p in template.split(";") if p.strip()]
        if not parts:
            return
        logger.info("RigNet.send_ctcss_cat: tone_hz=%s cmd=%r", tone_hz, template)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self._TIMEOUT)
            sock.connect((self._host, self._port))
            # Use a short recv timeout: the w command may return "?;" (rig CAT error),
            # empty string, or RPRT 0. We only care that the bytes were sent, not the
            # rig's response, so drain the buffer without blocking on slow rigs.
            sock.settimeout(1.0)
            for part in parts:
                cmd = f"w {part};"
                logger.info("RigNet.send_ctcss_cat: sending %r", cmd)
                sock.sendall((cmd + "\n").encode())
                with contextlib.suppress(OSError):
                    sock.recv(256)
            sock.close()
        except Exception as exc:
            logger.error("RigNet.send_ctcss_cat: %s", exc)

    def send_mode_only(self, dl_mode: str, ul_mode: str) -> None:
        """Set mode on both VFOs via an independent TCP connection.

        FT-991/FT-991A path (ctcss_method == "ft991"):
          Opens a fresh independent socket; main connection is kept alive.
          MD0{code};           — set VFO-A (DL) mode via rigctld w command
          SV; MD0{code}; SV;  — swap to VFO-B, set UL mode, swap back
          Each command waits for RPRT in the response before proceeding.
          2-second per-command timeout (SV may not return RPRT on some firmwares).

        FTX-1F / generic path:
          Opens a fresh socket (main socket disconnected by the caller).
          V Sub → M {ul} 0 → V Main → M {dl} 0
          On S 1 Main split: Sub=TX (uplink), Main=RX (downlink).
        """
        if self._ctcss_method == "ft991":
            # Default to FM ("4") for modes absent from _FT991_MODE_MAP (e.g.
            # SSTV, SSDV, AFSK, DOKA, FSK) so the rig always lands on a
            # sensible receive mode. Mirrors the Direct-mode counterpart
            # (_apply_mode_and_ctcss_cat_ft991's own .get(mode, "4")) — without
            # this default, an unmapped mode silently sent no CAT command at
            # all, leaving the rig parked in whatever mode a previous
            # transponder selection had set (e.g. stuck in CW after testing a
            # CW-mode transponder, then switching to an APRS/SSTV one).
            dl_code = _FT991_MODE_MAP.get(dl_mode, "4")
            ul_code = _FT991_MODE_MAP.get(ul_mode, "4")
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(self._TIMEOUT)
                sock.connect((self._host, self._port))
                sock.settimeout(2.0)  # short per-command timeout; SV may not send RPRT

                def _w(cmd: str) -> None:
                    sock.sendall(f"w {cmd}\n".encode())
                    buf = b""
                    with contextlib.suppress(OSError):
                        while b"RPRT" not in buf:
                            chunk = sock.recv(256)
                            if not chunk:
                                break
                            buf += chunk

                if dl_code:
                    _w(f"MD0{dl_code};")
                if ul_code:
                    _w("SV;")
                    _w(f"MD0{ul_code};")
                    _w("SV;")
                sock.close()
                logger.info("RigNet: FT-991 mode dl=%s ul=%s", dl_mode, ul_mode)
            except Exception as exc:
                logger.warning("RigNet: FT-991 mode send failed: %s", exc)
            return
        # Default to FM for modes absent from _SATNOGS_TO_RIGCTLD_MODE (e.g.
        # SSTV, SSDV, DOKA, FSK) — see the ft991 branch above for why an
        # unmapped mode must never silently skip sending a CAT command.
        rigctld_ul = _SATNOGS_TO_RIGCTLD_MODE.get(ul_mode, "FM")
        rigctld_dl = _SATNOGS_TO_RIGCTLD_MODE.get(dl_mode, "FM")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self._TIMEOUT)
            sock.connect((self._host, self._port))

            def _send_recv(cmd: str) -> str:
                sock.sendall((cmd + "\n").encode())
                buf = b""
                with contextlib.suppress(OSError):
                    while b"RPRT" not in buf:
                        chunk = sock.recv(256)
                        if not chunk:
                            break
                        buf += chunk
                resp = buf.decode(errors="replace").strip()
                if resp and "RPRT 0" not in resp:
                    logger.warning("RigNet.send_mode_only: %r -> %r", cmd, resp)
                return resp

            # For icom satmode rigs establish split before setting modes.
            # Same-band (V/V or U/U): S 1 VFOB → normal split (VFOA=RX, VFOB=TX).
            # Cross-band: S 1 Main → satmode (Main=RX, Sub=TX).
            is_satmode_rig = self._satmode or self._is_satmode_rig
            is_yaesu_cat = self._ctcss_method in ("ftx1", "ft991")
            if is_satmode_rig:
                if self._is_same_band:
                    # Same-band (V/V or U/U): normal split, VFOB=TX
                    _send_recv("S 1 VFOB")
                    ul_vfo_v = "VFOB"
                    ul_vfo_set = "VFOB"
                    dl_vfo_v = "VFOA"
                else:
                    # Cross-band: satmode, Sub=TX
                    _send_recv("S 1 Main")
                    ul_vfo_v = "Sub"
                    ul_vfo_set = "Sub"
                    dl_vfo_v = "Main"
            elif is_yaesu_cat:
                # FTX-1F/FT-991A: Sub/Main naming, confirmed working for
                # these two models' rigctld backend (see _init_vfo()).
                ul_vfo_v = "Sub"
                ul_vfo_set = "Sub"
                dl_vfo_v = "Main"
            else:
                # Other non-satmode rigs (e.g. IC-705): no true Main/Sub
                # concept — "Main"/"Sub" gets misparsed and inverts RX/TX
                # (confirmed live). Use plain VFOA/VFOB instead.
                ul_vfo_v = "VFOB"
                ul_vfo_set = "VFOB"
                dl_vfo_v = "VFOA"

            if self._vfo_mode:
                # Extended rigctld protocol: VFO is specified inline.
                if rigctld_ul:
                    _send_recv(f"\\set_mode {ul_vfo_set} {rigctld_ul} 0")
                if rigctld_dl:
                    _send_recv(f"\\set_mode {dl_vfo_v} {rigctld_dl} 0")
            else:
                # Legacy rigctld protocol: switch active VFO then set mode.
                if rigctld_ul:
                    _send_recv(f"V {ul_vfo_v}")
                    _send_recv(f"M {rigctld_ul} 0")
                if rigctld_dl:
                    _send_recv(f"V {dl_vfo_v}")
                    _send_recv(f"M {rigctld_dl} 0")

            # For non-satmode rigs the V <dl_vfo_v> above leaves TX on the RX
            # VFO.  Re-send split init so TX returns to the uplink VFO
            # immediately.
            if not is_satmode_rig:
                if is_yaesu_cat:
                    split_cmd = "S 1 VFOB" if self._is_same_band else "S 1 Main"
                else:
                    split_cmd = "S 1 VFOB"
                _send_recv(split_cmd)

            sock.close()
            logger.info("RigNet: send_mode_only dl=%s ul=%s done", dl_mode, ul_mode)
        except Exception as exc:
            logger.warning("RigNet: send_mode_only failed: %s", exc)

    def _send_freq_preset_independent(self) -> None:
        """Stage 1: write DL/UL frequencies via independent TCP socket.

        Anchors IC-9100/9700 SAT mode Main/Sub band assignment BEFORE
        send_mode_only() and _apply_ctcss_civ_direct() are called.
        Uses F (Main=DL) and I (Sub/TX=UL), matching the Doppler cycle.
        Called inside apply_transponder_state immediately after
        _send_split_init_independent() so SAT mode is already established.

        Generic rigs (e.g. IC-705): F/I write to the correct VFO internally
        (confirmed live via readback), but the display doesn't refresh to
        show it unless VFOA is explicitly reselected afterward — same
        display-refresh quirk as _send_split_init_independent().
        """
        if not self._transponder_dl_hz and not self._transponder_ul_hz:
            return
        logger.info(
            "RigNet: freq preset DL=%.3fMHz UL=%.3fMHz via independent socket",
            (self._transponder_dl_hz or 0) / 1e6,
            (self._transponder_ul_hz or 0) / 1e6,
        )
        is_generic = not (self._satmode or self._is_satmode_rig) and self._ctcss_method not in (
            "ftx1",
            "ft991",
        )
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self._TIMEOUT)
            sock.connect((self._host, self._port))
            sock.settimeout(2.0)

            def _send_recv(cmd: str) -> None:
                sock.sendall((cmd + "\n").encode())
                buf = b""
                with contextlib.suppress(OSError):
                    while b"RPRT" not in buf:
                        chunk = sock.recv(256)
                        if not chunk:
                            break
                        buf += chunk

            if self._transponder_dl_hz:
                _send_recv(f"F {int(self._transponder_dl_hz)}")
            if self._transponder_ul_hz:
                _send_recv(f"I {int(self._transponder_ul_hz)}")
            if is_generic:
                _send_recv("V VFOA")
            sock.close()
            logger.info("RigNet: freq preset done")
        except Exception as exc:
            logger.error("RigNet: freq preset failed: %s", exc)

    def read_dl_ul_independent(self) -> tuple[float, float] | None:
        """Read live DL/UL frequencies via a fresh, independent TCP socket.

        For Lock (L button) dial feedback (see MainWindow._lock_watch_cycle())
        before Connect has been pressed -- there is no persistent self._sock
        to read from yet, so this opens a short-lived connection, sends
        "S 1 Main" (idempotent -- matches the production connect sequence,
        the only command besides plain F/I ever confirmed live not to
        disturb the rig's Main/Sub role assignment) followed by read-only
        "f"/"i", and closes.

        Confirmed live (2026-07-15, FTX-1F): unlike a connection that has
        already sent "F"/"I" writes, a read-only "S 1 Main" -> "f" -> "i"
        sequence returns the correct DL/UL values from the very first read
        -- no self-heal delay, no wrong-VFO reading.

        Only meaningful for Yaesu-CAT NET-mode rigs (ctcss_method "ftx1" /
        "ft991") -- the only configuration this read mechanism has been
        verified against. Returns None for any other rig, or on any I/O
        failure.
        """
        if self._ctcss_method not in ("ftx1", "ft991"):
            return None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self._TIMEOUT)
            sock.connect((self._host, self._port))
            sock.settimeout(2.0)

            def _send_recv(cmd: str, is_query: bool) -> str:
                sock.sendall((cmd + "\n").encode())
                buf = b""
                with contextlib.suppress(OSError):
                    while True:
                        chunk = sock.recv(256)
                        if not chunk:
                            break
                        buf += chunk
                        if is_query:
                            if buf.endswith(b"\n"):
                                break
                        elif b"RPRT" in buf:
                            break
                return buf.decode(errors="replace").strip()

            _send_recv("S 1 Main", is_query=False)
            f_resp = _send_recv("f", is_query=True)
            i_resp = _send_recv("i", is_query=True)
            sock.close()
            dl = float(f_resp.splitlines()[0])
            ul = float(i_resp.splitlines()[0])
            return dl, ul
        except (OSError, ValueError, IndexError) as exc:
            logger.warning("RigNet: read_dl_ul_independent failed: %s", exc)
            return None

    def _send_split_init_independent(self) -> None:
        """Send satmode/split init via a fresh TCP socket (mirrors Direct-mode set_func(SATMODE,1)).

        Called at transponder selection time so that CTCSS is always set AFTER
        satmode is established — the same order as HamlibDirectController's
        _apply_mode_and_ctcss_hamlib().  If connect() later re-sends S 1 Main,
        the rig is already in satmode and will not reset the CTCSS state.

        Non-satmode, non-Yaesu rigs (e.g. IC-705) have no true Main/Sub VFO
        concept, so "S 1 Main" gets misparsed and inverts RX/TX (confirmed
        live) — use plain VFOA/VFOB split instead. See _init_vfo() for the
        satmode/FTX-1F/FT-991A cases this preserves unchanged.
        """
        is_satmode_rig = self._satmode or self._is_satmode_rig
        is_yaesu_cat = self._ctcss_method in ("ftx1", "ft991")
        is_generic = not is_satmode_rig and not is_yaesu_cat
        use_vfob_split = (is_satmode_rig and self._is_same_band) or is_generic
        cmd = "S 1 VFOB" if use_vfob_split else "S 1 Main"
        logger.info("RigNet: pre-connect split init (%s) via independent socket", cmd)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self._TIMEOUT)
            sock.connect((self._host, self._port))
            sock.settimeout(2.0)

            def _send_recv(c: str) -> bytes:
                sock.sendall((c + "\n").encode())
                buf = b""
                with contextlib.suppress(OSError):
                    while b"RPRT" not in buf:
                        chunk = sock.recv(256)
                        if not chunk:
                            break
                        buf += chunk
                return buf

            buf = _send_recv(cmd)
            if is_generic:
                # IC-705 (and generic rigs like it): the frequency/mode
                # writes below land on the correct VFO internally, but the
                # display doesn't refresh unless VFOA is explicitly
                # reselected afterward (confirmed live — same display-
                # refresh quirk fixed for Direct mode elsewhere in this
                # file). Restore VFOA so the display matches.
                _send_recv("V VFOA")
            sock.close()
            logger.info(
                "RigNet: pre-connect split init done -> %r",
                buf.decode(errors="replace").strip(),
            )
        except Exception as exc:
            logger.error("RigNet: pre-connect split init failed: %s", exc)

    def apply_transponder_state(self, dl_mode: str, ul_mode: str, ctcss_hz: float) -> None:
        """Apply mode and CTCSS sequentially in the same thread for NET-mode rigs.

        Satmode rigs (IC-9100 / IC-9700 etc.) via rigctld:
          Holds _cmd_lock for the entire operation so the Doppler cycle cannot
          send frequency commands to rigctld while mode and CTCSS are being set.
          1. _send_split_init_independent — S 1 Main (or S 1 VFOB) to establish
             satmode BEFORE mode/CTCSS, matching Direct-mode order.
          2. send_mode_only — sets DL/UL modes via rigctld.
          3. _apply_ctcss_civ_direct — sets CTCSS encoder via rigctld commands.
          This order ensures the T mark appears immediately at transponder
          selection, same as Direct mode.

        Non-satmode rigs:
          Falls back to the base-class default (send_mode_only via rigctld,
          then set_ctcss_tone via rigctld L command).
        """
        if self.is_satmode:
            logger.info(
                "RigNet: apply_transponder_state (satmode) dl=%s ul=%s ctcss=%.1f",
                dl_mode,
                ul_mode,
                ctcss_hz,
            )
            # Cache modes for Stage-2 resend.
            self._current_dl_mode = dl_mode
            self._current_ul_mode = ul_mode
            # Store so connect() and Stage-2 can re-apply CTCSS after SAT mode is confirmed.
            # IC-9700 keeps Normal-mode and SAT-mode CTCSS registers separately;
            # writing before connect() lands in Normal mode, so we resend after
            # the first live F/I write locks the band assignment.
            self._pending_ctcss_hz = ctcss_hz if ctcss_hz > 0.0 else None
            with self._cmd_lock:
                self._send_split_init_independent()
                # Stage 1: write DL/UL frequencies BEFORE modes/CTCSS so that
                # IC-9100/9700 SAT mode Main/Sub band assignment is anchored to
                # the new satellite before mode bytes are sent.
                self._send_freq_preset_independent()
                self.send_mode_only(dl_mode, ul_mode)
                self._apply_ctcss_civ_direct(ctcss_hz)
            # Stage 2: after connect() + first Doppler I write, re-confirm
            # mode/CTCSS now that freq anchor is established on the live connection.
            self._pending_mode_net = True
        else:
            # Non-satmode rigs (FTX-1F, FT-991A, etc.):
            # send_mode_only uses V Sub/V Main which leaves TX on Main after the
            # last V Main command.  Bracket the mode set with split init so TX
            # stays on Sub (uplink) both before and after mode is applied.
            logger.info(
                "RigNet: apply_transponder_state dl=%s ul=%s ctcss=%.1f",
                dl_mode,
                ul_mode,
                ctcss_hz,
            )
            self._send_split_init_independent()
            self.send_mode_only(dl_mode, ul_mode)
            self._send_split_init_independent()
            self.set_ctcss_tone(ctcss_hz)

    def get_rig_info(self) -> RigInfo | None:
        if not self.is_connected:
            return None
        return RigInfo(
            model_id=0,
            model_name=f"{self._host}:{self._port}",
            port=f"{self._host}:{self._port}",
            baud_rate=0,
            state=self.state,
        )


# rigctld mode name mapping
_SATNOGS_TO_RIGCTLD_MODE: dict[str, str] = {
    "DIGITALVOICE": "FM",
    "FM": "FM",
    "USB": "USB",  # rigctld-style name used by some SatNOGS entries; placed first
    "SSB": "USB",  # canonical SatNOGS name; wins in reverse map
    "LSB": "LSB",
    "CW": "CW",
    "CW-R": "CWR",
    "BPSK": "PKTUSB",
    "AFSK": "PKTFM",
    "AM": "AM",
    "USB-D": "PKTUSB",  # data mode, e.g. FT4 calling freqs
    "LSB-D": "PKTLSB",
}
_RIGCTLD_MODE_TO_SATNOGS: dict[str, str] = {v: k for k, v in _SATNOGS_TO_RIGCTLD_MODE.items()}


# ---------------------------------------------------------------------------
# Abstract base class — RotatorController
# ---------------------------------------------------------------------------


class RotatorController(ABC):
    """Abstract base class for rotator control."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = RigState.DISCONNECTED
        self._rotor_state = RotatorState()

    @abstractmethod
    def connect(self) -> bool:
        """Establish a connection."""

    @abstractmethod
    def disconnect(self) -> None:
        """Disconnect."""

    @property
    def is_connected(self) -> bool:
        """Whether currently connected."""
        with self._lock:
            return self._state == RigState.CONNECTED

    @abstractmethod
    def set_position(self, azimuth_deg: float, elevation_deg: float) -> bool:
        """Set the azimuth and elevation in degrees."""

    @abstractmethod
    def get_position(self) -> RotatorState:
        """Return the current azimuth and elevation."""

    @abstractmethod
    def stop(self) -> bool:
        """Stop rotation."""

    @abstractmethod
    def park(self) -> bool:
        """Return to the home position."""


# ---------------------------------------------------------------------------
# HamlibRotatorController
# ---------------------------------------------------------------------------


class HamlibRotatorController(RotatorController):
    """
    Rotator controller using Hamlib.

    Supports both direct serial connection (equivalent to HamlibDirect) and
    NET connection (rotctld). When net_mode=True, connects to rotctld over TCP.
    """

    _CATCH_UP_THRESHOLD: float = 5.0  # degrees; switch to normal tracking when within this
    _CATCH_UP_TIMEOUT: float = 60.0  # seconds; resend P command if catch-up takes too long

    def __init__(
        self,
        model_id: int = 1,
        port: str = "/dev/ttyUSB0",
        baud_rate: int = 9600,
        *,
        net_mode: bool = False,
        net_host: str = "localhost",
        net_port: int = 4533,
    ) -> None:
        super().__init__()
        self._model_id = model_id
        self._port = port
        self._baud_rate = baud_rate
        self._net_mode = net_mode
        self._net_host = net_host
        self._net_port = net_port
        self._rot: Any = None
        self._hamlib: Any = None  # Hamlib module, set lazily in connect()
        self._sock: socket.socket | None = None
        self._last_az: float | None = None  # last commanded AZ for shortest-path calc
        self._catching_up: bool = False  # True while rotator is moving to initial position
        self._catch_up_start_time: float | None = None  # monotonic time when catch-up started

    def connect(self) -> bool:
        """Connect to the rotator."""
        with self._lock:
            if self._state == RigState.CONNECTED:
                return True
            self._state = RigState.CONNECTING

        try:
            if self._net_mode:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                sock.connect((self._net_host, self._net_port))
                self._sock = sock
            elif HAMLIB_AVAILABLE:
                import Hamlib as _H  # lazy — avoids Qt TLS collision at startup

                self._hamlib = _H
                rot = _H.Rot(self._model_id)
                logger.info(
                    "Rotator: creating controller port=%s model=%s",
                    self._port,
                    self._model_id,
                )
                rot.set_conf("rot_pathname", self._port)
                rot.set_conf("serial_speed", str(self._baud_rate))
                rot.open()
                self._rot = rot
            else:
                self._rot = _MockRotator()

            with self._lock:
                self._state = RigState.CONNECTED
            self._last_az = None
            self._catching_up = False
            self._catch_up_start_time = None
            logger.info("Rotator: connected")
            return True
        except Exception as exc:
            with self._lock:
                self._state = RigState.ERROR
            logger.error("Rotator: connect failed — %s", exc)
            return False

    def disconnect(self) -> None:
        """Disconnect the rotator."""
        try:
            if self._net_mode and self._sock:
                self._sock.close()
            elif self._rot is not None and self._hamlib is not None:
                self._rot.close()
        except Exception:
            pass
        finally:
            self._rot = None
            self._sock = None
            with self._lock:
                self._state = RigState.DISCONNECTED

    def _send_p(self, az: float, el: float) -> None:
        """Send the P command and discard the RPRT response to keep the socket buffer clean."""
        if self._net_mode and self._sock:
            self._sock.sendall(f"P {az:.1f} {el:.1f}\n".encode())
            with contextlib.suppress(Exception):
                self._sock.recv(256)  # discard RPRT 0
        elif self._rot is not None:
            self._rot.set_position(az, el)
        with self._lock:
            self._rotor_state.azimuth_deg = az
            self._rotor_state.elevation_deg = el
            self._rotor_state.is_moving = True

    def set_position(self, azimuth_deg: float, elevation_deg: float) -> bool:
        """Rotate to the specified azimuth and elevation.

        Four phases:
        1. First call after connect (_last_az is None): send P command to current
           satellite position and enter catch-up mode.
        2. Catch-up mode: poll the rotator position each cycle.
           - Within _CATCH_UP_THRESHOLD degrees: exit catch-up, start normal tracking.
           - Timeout (_CATCH_UP_TIMEOUT seconds): resend P command and restart timer.
           - Otherwise: return and wait for the next cycle.
        3. Normal tracking: send P command with current satellite AZ/EL each cycle.
        4. 0-degree wrap (large AZ jump): re-enter catch-up and send P immediately.
        """
        if not self.is_connected:
            return False
        try:
            el_cmd = max(0.0, min(90.0, elevation_deg))

            if self._last_az is None:
                self._send_p(azimuth_deg, el_cmd)
                self._catching_up = True
                self._catch_up_start_time = time.monotonic()
                self._last_az = azimuth_deg
                logger.info("Rotator: initial jump to az=%.1f el=%.1f", azimuth_deg, el_cmd)
                return True

            if self._catching_up:
                current = self.get_position()
                rot_az = current.azimuth_deg
                sat_az = azimuth_deg

                az_diff = abs(rot_az - sat_az)
                if az_diff > 180:
                    az_diff = 360.0 - az_diff

                if az_diff <= self._CATCH_UP_THRESHOLD:
                    self._catching_up = False
                    self._catch_up_start_time = None
                    logger.info("Rotator: caught up at rot=%.1f sat=%.1f", rot_az, sat_az)
                    # Fall through to normal tracking below
                elif time.monotonic() - (self._catch_up_start_time or 0.0) > self._CATCH_UP_TIMEOUT:
                    self._send_p(azimuth_deg, el_cmd)
                    self._catch_up_start_time = time.monotonic()
                    self._last_az = azimuth_deg
                    logger.info("Rotator: catch-up timeout, retrying az=%.1f", azimuth_deg)
                    return True
                else:
                    return True  # Still waiting for rotator to reach target

            last = self._last_az
            crossed_zero = (last > 270 and azimuth_deg < 90) or (last < 90 and azimuth_deg > 270)

            if crossed_zero:
                self._catching_up = True
                self._catch_up_start_time = time.monotonic()
                self._last_az = azimuth_deg
                self._send_p(azimuth_deg, el_cmd)
                logger.info(
                    "Rotator: 0-degree wrap %.1f->%.1f, re-entering catch-up",
                    last,
                    azimuth_deg,
                )
                return True

            self._last_az = azimuth_deg
            self._send_p(azimuth_deg, el_cmd)
            return True
        except Exception as exc:
            logger.error("Rotator.set_position: %s", exc)
            return False

    def get_position(self) -> RotatorState:
        """Return the current azimuth and elevation."""
        if not self.is_connected:
            return RotatorState()
        try:
            if self._net_mode and self._sock:
                self._sock.sendall(b"p\n")
                data = self._sock.recv(512).decode(errors="replace")
                values: list[float] = []
                for line in data.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("RPRT"):
                        with contextlib.suppress(ValueError):
                            values.append(float(line))
                if len(values) >= 2:
                    with self._lock:
                        self._rotor_state.azimuth_deg = values[0]
                        self._rotor_state.elevation_deg = values[1]
            elif self._rot is not None:
                az, el = self._rot.get_position()
                with self._lock:
                    self._rotor_state.azimuth_deg = float(az)
                    self._rotor_state.elevation_deg = float(el)
        except Exception as exc:
            logger.error("Rotator.get_position: %s", exc)

        with self._lock:
            return RotatorState(
                azimuth_deg=self._rotor_state.azimuth_deg,
                elevation_deg=self._rotor_state.elevation_deg,
                is_moving=self._rotor_state.is_moving,
            )

    def stop(self) -> bool:
        """Stop rotation."""
        if not self.is_connected:
            return False
        try:
            if self._net_mode and self._sock:
                self._sock.sendall(b"S\n")
            elif self._rot is not None:
                self._rot.stop()
            with self._lock:
                self._rotor_state.is_moving = False
            return True
        except Exception as exc:
            logger.error("Rotator.stop: %s", exc)
            return False

    def park(self) -> bool:
        """Return to the home position (rotctld: K command)."""
        if not self.is_connected:
            return False
        try:
            if self._net_mode and self._sock:
                self._sock.sendall(b"K\n")
            elif self._rot is not None:
                self._rot.park()
            return True
        except Exception as exc:
            logger.error("Rotator.park: %s", exc)
            return False


# ---------------------------------------------------------------------------
# HamlibVersionChecker
# ---------------------------------------------------------------------------
# SdrRigAdapter — wraps an SdrDevice as a RigController slot
# ---------------------------------------------------------------------------


class SdrRigAdapter(RigController):
    """
    Adapter that presents an SDR device as a Rig 1 / Rig 2 controller.

    The SDR does not transmit, so set_mode / set_ctcss_tone / set_dcs_code are
    no-ops.  set_frequency / set_vfo_frequencies update the SDR center frequency
    so the Doppler-correction loop drives the SDR tuning.

    is_sdr = True lets the UI distinguish SDR slots from Hamlib rigs.
    """

    is_sdr: bool = True

    def __init__(self) -> None:
        super().__init__()
        # Lazily imported to avoid loading SoapySDR at startup
        self._sdr_device: SdrDevice | None = None
        self._pipeline: SDRPipeline | None = None
        self._device_info: SdrDeviceInfo | None = None
        # Audio params applied after open()
        self._sample_rate_hz: float = 2_400_000
        self._ppm: float = 0.0
        self._gain_auto: bool = True
        self._gain_db: float = 40.0
        self._bias_tee: bool = False

    def set_device_info(self, info: SdrDeviceInfo) -> None:
        """Attach an SdrDeviceInfo before calling connect()."""
        self._device_info = info

    def set_audio_params(
        self,
        sample_rate_hz: float = 2_400_000,
        ppm: float = 0.0,
        gain_auto: bool = True,
        gain_db: float = 40.0,
        bias_tee: bool = False,
    ) -> None:
        """Store sample rate, PPM correction, gain and Bias-T settings applied on connect()."""
        self._sample_rate_hz = sample_rate_hz
        self._ppm = ppm
        self._gain_auto = gain_auto
        self._gain_db = gain_db
        self._bias_tee = bias_tee

    def attach_pipeline(self, pipeline: SDRPipeline) -> None:
        """Attach a running SDRPipeline (set after connect succeeds)."""
        self._pipeline = pipeline

    def connect(self) -> bool:
        """Open the SoapySDR device. Returns True on success."""
        if self._device_info is None:
            logger.warning("SdrRigAdapter: no device_info set")
            return False
        try:
            from sdr.device import SdrDevice

            dev = SdrDevice(self._device_info)
            logger.info(
                "SdrRigAdapter.connect: sample_rate=%.0f ppm=%g gain_auto=%s "
                "gain_db=%g bias_tee=%s",
                self._sample_rate_hz,
                self._ppm,
                self._gain_auto,
                self._gain_db,
                self._bias_tee,
            )
            if dev.open():
                # Apply stored audio settings immediately after open
                dev.set_sample_rate(self._sample_rate_hz)
                dev.set_ppm(self._ppm)
                if self._gain_auto:
                    dev.set_gain_auto()
                else:
                    dev.set_gain_db(self._gain_db)
                dev.set_bias_tee(self._bias_tee)
                self._sdr_device = dev
                with self._lock:
                    self._state = RigState.CONNECTED
                logger.info("SDR connected: %s", self._device_info.display_name)
                return True
        except Exception:
            logger.exception("SdrRigAdapter.connect failed")
        with self._lock:
            self._state = RigState.ERROR
        return False

    def disconnect(self) -> None:
        """Stop the pipeline and close the device."""
        if self._pipeline is not None:
            try:
                self._pipeline.stop()
                self._pipeline.wait(3000)
            except Exception:
                pass
            self._pipeline = None
        if self._sdr_device is not None:
            with contextlib.suppress(Exception):
                self._sdr_device.close()
            self._sdr_device = None
        with self._lock:
            self._state = RigState.DISCONNECTED

    def set_frequency(self, freq_hz: float, vfo: str = "VFOA") -> bool:
        """Retune the SDR center frequency (used by Doppler correction loop)."""
        if self._sdr_device is not None:
            return self._sdr_device.set_center_freq(freq_hz)
        return False

    def get_frequency(self, vfo: str = "VFOA") -> float:
        if self._sdr_device is not None:
            return self._sdr_device.center_freq
        return -1.0

    def set_vfo_frequencies(
        self,
        vfoa_hz: float | None,
        vfob_hz: float | None,
    ) -> bool:
        """For SDR, only the downlink (vfoa_hz) matters."""
        if vfoa_hz is not None:
            return self.set_frequency(vfoa_hz)
        return True

    def set_mode(self, mode: str, passband_hz: int = 0, vfo: str = "VFOA") -> bool:
        """SDR mode is controlled via SDR Control tab, not Hamlib."""
        return True

    def get_mode(self, vfo: str = "VFOA") -> str:
        return self._freq_state.mode

    def set_ctcss_tone(self, tone_hz: float) -> bool:
        return True  # SDR RX only — no CTCSS

    def set_dcs_code(self, code: int) -> bool:
        return True

    def set_vfo(self, vfo: str) -> bool:
        return True

    def get_rig_info(self) -> RigInfo | None:
        """Return a minimal RigInfo for the SDR device (RX-only)."""
        if self._device_info is None:
            return None
        return RigInfo(
            model_id=0,
            model_name=self._device_info.display_name,
            port="SoapySDR",
            baud_rate=0,
            state=self._state,
        )

    @property
    def sdr_device(self) -> SdrDevice | None:
        return self._sdr_device


# ---------------------------------------------------------------------------


class HamlibVersionChecker:
    """
    Fetches the installed Hamlib version and compares it against the latest
    GitHub release, returning a warning when an upgrade is available.
    """

    _GITHUB_API = "https://api.github.com/repos/Hamlib/Hamlib/releases/latest"

    def get_installed_version(self) -> str:
        """Return the installed Hamlib version string, or "not installed" when absent."""
        if HAMLIB_AVAILABLE:
            try:
                import Hamlib as _H

                return str(_H.cvar.hamlib_version)
            except Exception:
                return "unknown"
        return "not installed"

    async def check_version(self, timeout: float = 10.0) -> VersionInfo:
        """
        Check the latest version via the GitHub API and return a VersionInfo.

        When the network is unavailable, returns the installed version only
        with is_outdated=False (no warning).
        """
        installed = self.get_installed_version()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    self._GITHUB_API,
                    headers={"Accept": "application/vnd.github+json"},
                )
                resp.raise_for_status()
                data = resp.json()
                latest = str(data.get("tag_name", "")).lstrip("v")
                release_url = str(data.get("html_url", ""))
        except Exception as exc:
            logger.warning("HamlibVersionChecker: could not fetch latest version — %s", exc)
            return VersionInfo(installed=installed, latest=installed, is_outdated=False)

        is_outdated = installed not in ("not installed", "unknown") and self._version_lt(
            installed, latest
        )
        return VersionInfo(
            installed=installed,
            latest=latest,
            is_outdated=is_outdated,
            release_url=release_url,
        )

    @staticmethod
    def _version_lt(a: str, b: str) -> bool:
        """Return True when version string a is less than b (semantic versioning assumed)."""

        def _parts(v: str) -> tuple[int, ...]:
            parts = []
            for seg in v.split(".")[:3]:
                try:
                    parts.append(int(seg))
                except ValueError:
                    parts.append(0)
            while len(parts) < 3:
                parts.append(0)
            return tuple(parts)

        return _parts(a) < _parts(b)


# ---------------------------------------------------------------------------
# Internal mock classes (for environments without Hamlib)
# ---------------------------------------------------------------------------


class _MockRig:
    """Stub for environments where python-hamlib is unavailable. Used in tests and CI."""

    def __init__(self, model_id: int) -> None:
        self._model_id = model_id
        self._freq: float = 145_800_000.0
        self._mode: int = 32  # RIG_MODE_FM
        self._passband: int = 15000

    class caps:  # noqa: N801
        model_name = "Mock Rig"

    def set_freq(self, vfo: int, freq: float) -> None:
        self._freq = freq

    def get_freq(self, vfo: int) -> float:
        return self._freq

    def set_mode(self, vfo: int, mode: int, passband: int) -> None:
        self._mode = mode
        self._passband = passband

    def get_mode(self, vfo: int) -> tuple[int, int]:
        return self._mode, self._passband

    def set_split_vfo(self, vfo: int, split: int, tx_vfo: int) -> None:
        pass

    def set_split_freq(self, vfo: int, freq: float) -> None:
        pass

    def set_func(self, vfo: int, func: int, status: int) -> None:
        pass

    def set_level(self, vfo: int, level: int, value: int) -> None:
        pass

    def set_vfo(self, vfo: int) -> None:
        pass

    def close(self) -> None:
        pass


class _MockRotator:
    """Rotator stub for environments where python-hamlib is unavailable."""

    def __init__(self) -> None:
        self._az: float = 0.0
        self._el: float = 0.0

    def set_position(self, az: float, el: float) -> None:
        self._az = az
        self._el = el

    def get_position(self) -> tuple[float, float]:
        return self._az, self._el

    def stop(self) -> None:
        pass

    def park(self) -> None:
        pass

    def close(self) -> None:
        pass
