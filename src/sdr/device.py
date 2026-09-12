"""
SoapySDR device abstraction.

SdrDeviceInfo  — Enumerated device descriptor (driver, label, serial, etc.)
SdrDevice      — Thin wrapper around a SoapySDR.Device instance.

All public methods are thread-safe (protected by an internal lock).
When SoapySDR is not installed, SdrDevice.enumerate() returns [] and
any instantiation raises RuntimeError so callers can degrade gracefully.

USB fallback:
  When SoapySDR is absent, enumerate_usb() uses pyusb to scan for known
  SDR VID/PID pairs and returns a list of SdrDeviceInfo with driver=None.
  This is used by the SDR Device Installation dialog to identify devices
  even before the driver is installed.

Windows RTL-SDR direct path:
  On Windows, SoapyRTLSDR's C++ constructor succeeds but
  SoapySDR::Device::make() rejects it at the ABI check layer, resulting in
  "no match".  When driver=="rtlsdr" on win32 we bypass SoapySDR entirely
  and call librtlsdr.dll via ctypes through RtlSdrDirectDevice, which is
  duck-type compatible with SoapySDR.Device so the rest of SdrDevice is
  unchanged.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import ipaddress
import logging
import queue
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


def _resolve_ipv4_host(host: str) -> str:
    """Best-effort resolve `host` to a literal IPv4 address.

    SoapyRemote's C++ client tries only the first address getaddrinfo()
    returns and does not fall back to a later one on failure.  On macOS,
    resolving a ".local" mDNS name returns an IPv6 link-local address
    (fe80::...) *before* the IPv4 one; SoapyRemote's connect() to that raw
    link-local address (no interface scope attached) fails immediately with
    "Operation timed out" -- even though the exact same name resolves fine
    for everything else (ping, dscacheutil, Python sockets, which all try
    every address getaddrinfo() returns in order). Resolving to IPv4
    ourselves before handing the address to SoapySDR works around it.
    Returns `host` unchanged if it is already a literal IP address, or if
    resolution fails for any reason, so this is never worse than doing
    nothing.
    """
    try:
        ipaddress.ip_address(host)
        return host  # already a literal IPv4/IPv6 address
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
        return str(infos[0][4][0])
    except OSError:
        return host


def _resolve_remote_addr(remote_value: str) -> str:
    """Resolve the host part of a SoapyRemote `remote` arg value to IPv4.

    Accepts "host", "host:port", or "scheme://host[:port]" and returns the
    same shape with the host resolved via _resolve_ipv4_host().
    """
    scheme = ""
    rest = remote_value
    if "://" in rest:
        scheme, rest = rest.split("://", 1)
        scheme += "://"
    host, sep, port = rest.partition(":")
    return f"{scheme}{_resolve_ipv4_host(host)}{sep}{port}"


try:
    import SoapySDR as _soapy_probe  # noqa: F401

    SOAPY_AVAILABLE: bool = True
except Exception:
    SOAPY_AVAILABLE = False

try:
    import usb.core as _usb_probe  # noqa: F401

    PYUSB_AVAILABLE: bool = True
except Exception:
    PYUSB_AVAILABLE = False

# Known SDR device VID/PID pairs for USB fallback detection
_KNOWN_USB_DEVICES: list[tuple[int, int, str, str]] = [
    (0x0BDA, 0x2838, "RTL-SDR V3 / Blog V3", "SoapyRTLSDR"),
    (0x0BDA, 0x2832, "RTL-SDR Blog V4", "SoapyRTLSDR"),
    (0x0BDA, 0x2837, "RTL-SDR (generic)", "SoapyRTLSDR"),
    (0x1D50, 0x6089, "HackRF One", "SoapyHackRF"),
    (0x1D50, 0x60A1, "AirSpy", "SoapyAirspy"),
    (0x1D50, 0x60A0, "AirSpy Mini", "SoapyAirspy"),
    (0x1DF7, 0x2500, "SDRplay RSP1", "SoapySDRPlay"),
    (0x1DF7, 0x3000, "SDRplay RSP1A", "SoapySDRPlay"),
]


@dataclass
class SdrDeviceInfo:
    """Descriptor returned by SdrDevice.enumerate()."""

    driver: str | None  # SoapySDR driver name, e.g. "rtlsdr", "hackrf"
    label: str  # Human-readable name
    serial: str  # Serial number or empty string
    hardware: str  # Hardware revision string
    args: dict[str, str] = field(default_factory=dict)  # Raw SoapySDR kwargs
    vid: int = 0  # USB VID (USB fallback only)
    pid: int = 0  # USB PID (USB fallback only)
    soapy_module: str = ""  # Suggested SoapySDR module name for installation
    # Real overall RX gain ceiling (dB), read from the device's own
    # getGainRange() when available (currently: query_remote_host() only —
    # see there for why). None means "not queried"; callers should fall back
    # to a driver-name-based guess rather than assume a narrow default, since
    # under-reporting silently discards gain values the user has set.
    gain_max_db: float | None = None
    # Whether the device has real hardware AGC (SoapySDR hasGainMode()),
    # read alongside gain_max_db in query_remote_host(). None means "not
    # queried"; callers should treat that as "assume supported" so the
    # Auto option is never hidden for a device we simply couldn't probe.
    supports_agc: bool | None = None

    @property
    def display_name(self) -> str:
        """Short name for UI dropdowns."""
        if self.serial:
            return f"{self.label} #{self.serial}"
        return self.label


# SoapySDR drivers that expose non-hardware devices (audio cards, test sinks).
# These are excluded from the Rig Settings SDR device list so users only see
# real RF receivers (RTL-SDR, HackRF, AirSpy, etc.).
#
# "remote" (SoapyRemote) is intentionally NOT excluded: it proxies a real RF
# receiver over the network (see "Add Remote Host..." in SDR Settings), and
# devices auto-discovered via SoapyRemote's LAN broadcast should show up like
# any other hardware device.
_NON_SDR_DRIVERS: frozenset[str] = frozenset({"audio", "null", "mircsdr"})

# Global lock: SoapySDR C++ layer is not re-entrant on Windows.
# All enumerate() and Device() calls must be serialised to prevent segfaults
# when multiple threads (Rig Settings, SDR Install dialog, pipeline) call
# SoapySDR concurrently.
_SOAPY_GLOBAL_LOCK: threading.Lock = threading.Lock()

# Process-level enumerate cache.  On Windows, calling SoapySDR.Device.enumerate()
# more than once per process can crash (segfault inside the native module loader).
# Cache the first successful result and return it on subsequent calls.
# Pass force=True (only from the Enumerate button) to bypass the cache.
_enumerate_cache: list[SdrDeviceInfo] | None = None

# SoapySDR's stream-result error code for a dropped-sample overflow (the
# driver's ring buffer filled before this process drained it).  Hardcoded
# rather than imported from the SoapySDR module so SdrDevice.read_samples()'s
# hot loop never pays a lazy-import cost -- this value (from SoapySDR's
# Errors.h) has been stable across the project's entire supported version
# range.
_SOAPY_SDR_OVERFLOW: int = -4

# SoapySDR's RX direction constant (from Types.h: SOAPY_SDR_TX=0, SOAPY_SDR_RX=1).
# Hardcoded for the same reason as _SOAPY_SDR_OVERFLOW above. This one is worth
# spelling out: every gain call in this class direction=0 -- which is TX, not
# RX -- until a 2026-09-11 investigation caught it. It went unnoticed because
# _apply_settings() (the open()-time gain setup) already imports SoapySDR and
# correctly uses SoapySDR.SOAPY_SDR_RX there, so only the *live* gain-change
# path (set_gain_db()/set_gain_auto(), and the new software AGC loop that
# calls setGain() continuously while streaming) was silently steering a TX
# gain element that these receive-only drivers don't actually use for RX --
# harmless for a single redundant post-open() call, but fatal for a loop that
# depends on it working every time.
_SOAPY_SDR_RX: int = 1

# Software AGC tuning (see SdrDevice._sw_agc_step()). Thresholds are the
# magnitude of a complex sample |I+jQ| (max possible sqrt(2) for full-scale
# I and Q). Measured against a real HackRF on a strong local FM broadcast
# station (2026-09-11): ADC clipping was observed starting around 0.98, with
# max gain (116 dB) driving ~65-93% of samples above that.
#
# An earlier version targeted a conservative, clipping-free 0.3-0.5 (chosen
# to protect a weak satellite signal's SNR, the primary use case). For a
# strong signal that's the technically "correct" level, but it left the
# demodulated audio much quieter than at max gain -- and per user testing,
# effectively inaudible against this app's FM demodulator, which is a
# narrowband design (see demodulator.py's NFM_DEVIATION) that can't cleanly
# render a wideband source either way. FM (unlike AM/SSB) only carries
# information in phase, not amplitude, so some hard limiting is standard
# behavior in real FM receivers, not pure signal destruction -- explicit
# user direction (2026-09-12) was to prioritize always producing an audible
# result over protecting headroom, i.e. run close to the observed clip
# point rather than well under it. _SW_AGC_LOW_PEAK sits below the target
# with a dead band between them so the loop doesn't hunt at the boundary;
# only sustained (_SW_AGC_RELEASE_S) headroom below LOW earns a gain
# increase back.
_SW_AGC_TARGET_PEAK: float = 1.1
_SW_AGC_LOW_PEAK: float = 0.9
_SW_AGC_STEP_DB: float = 6.0
_SW_AGC_RELEASE_S: float = 1.5
# A single read_samples() call only returns whatever arrived in one network
# datagram (~700 samples for a 2 Msps HackRF stream over SoapyRemote, not
# the much larger size actually requested), and a step's effect on the
# analog front end takes a few of those chunks to show up in what's being
# read. Without a cooldown, "fast attack" reacts to every one of those
# still-stale, still-clipping chunks and ratchets down far past the gain
# actually needed before the first step's effect is even visible -- verified
# on a real HackRF (2026-09-11): an uncooled loop overshot from 116 dB to
# 0 dB in ~3s against a signal that only needed ~32-44 dB. Bounding the
# attack rate (still much faster than the release side) fixes this.
_SW_AGC_ATTACK_COOLDOWN_S: float = 0.2


# ---------------------------------------------------------------------------
# RTL-SDR ctypes helpers
# ---------------------------------------------------------------------------


def _find_rtlsdr_dll() -> str | None:
    """Locate rtlsdr.dll on Windows, returning the full path or None."""
    search_dirs: list[str] = []
    if getattr(sys, "frozen", False):
        search_dirs.append(sys._MEIPASS)  # type: ignore[attr-defined]
    for env_var in ("SOAPY_SDR_ROOT", "SOAPY_SDR_PLUGIN_PATH"):
        val = __import__("os").environ.get(env_var, "")
        if val:
            search_dirs.append(str(Path(val).parent))
    plugin_path = __import__("os").environ.get("SOAPY_SDR_PLUGIN_PATH", "")
    if plugin_path:
        search_dirs.append(str(Path(plugin_path).parent.parent / "bin"))

    logger.info("[RTL-SDR diag] rtlsdr.dll search dirs: %s", search_dirs)
    for d in search_dirs:
        candidate = Path(d) / "rtlsdr.dll"
        if candidate.exists():
            logger.info("[RTL-SDR diag] rtlsdr.dll resolved path: %s", candidate)
            return str(candidate)
    found = ctypes.util.find_library("rtlsdr")
    logger.info("[RTL-SDR diag] rtlsdr.dll resolved path: %s", found)
    return found


def _find_hackrf_dll() -> str | None:
    """Locate hackrf.dll on Windows, returning the full path or None.

    Searches in priority order:
      1. PyInstaller _MEIPASS (_internal/)
      2. soapy_modules/ directory (where HackRFSupport.dll lives)
      3. SOAPY_SDR_PLUGIN_PATH parent (development environment)
      Tries exact name 'hackrf.dll' first, then glob 'hackrf*.dll' and
      'libhackrf*.dll' to handle versioned or differently-named DLLs from
      conda-forge (e.g. libhackrf.dll, hackrf-0.dll).
    """
    import os as _os

    search_dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = Path(sys._MEIPASS)  # type: ignore[attr-defined]
        search_dirs.append(meipass)
        # soapy_modules/ is a sibling of _MEIPASS in the installed layout
        search_dirs.append(meipass / "soapy_modules")
    plugin_path = _os.environ.get("SOAPY_SDR_PLUGIN_PATH", "")
    if plugin_path:
        pp = Path(plugin_path)
        search_dirs.append(pp.parent)  # _MEIPASS in dev
        search_dirs.append(pp)  # soapy_modules/ itself in dev

    # Exact-name candidates first (most common), then versioned/prefixed variants
    name_patterns = ["hackrf.dll", "hackrf-0.dll", "libhackrf.dll", "libhackrf-0.dll"]

    logger.info("[HackRF direct] hackrf.dll search dirs: %s", [str(d) for d in search_dirs])
    for d in search_dirs:
        for name in name_patterns:
            candidate = d / name
            if candidate.exists():
                logger.info("[HackRF direct] hackrf.dll found: %s", candidate)
                return str(candidate)
        # Glob fallback: catch any hackrf*.dll not matched above
        for match in sorted(d.glob("hackrf*.dll")):
            logger.info("[HackRF direct] hackrf.dll found via glob: %s", match)
            return str(match)

    found = ctypes.util.find_library("hackrf")
    logger.info("[HackRF direct] hackrf.dll search result: %s", found)
    return found


def _rtlsdr_ctypes_diagnostic() -> None:
    """Call rtlsdr_get_device_count() via ctypes and log the result.

    This runs BEFORE SoapySDR.Device() so we can confirm whether librtlsdr.dll
    itself can see the device through WinUSB at the Python level.  If count > 0
    here but SoapySDR still fails, the problem is inside SoapyRTLSDR's C++ code.
    If count == 0 here, libusb/WinUSB is the problem regardless of SoapyRTLSDR patches.
    """
    dll_path = _find_rtlsdr_dll()

    if dll_path is None:
        logger.warning("[RTL-SDR diag] rtlsdr.dll not found — cannot run ctypes diagnostic")
        return

    try:
        lib = ctypes.CDLL(dll_path)
    except OSError as exc:
        logger.warning("[RTL-SDR diag] Failed to load rtlsdr.dll via ctypes: %s", exc)
        return

    try:
        get_count = lib.rtlsdr_get_device_count
        get_count.restype = ctypes.c_uint32
        get_count.argtypes = []
        count = get_count()
        logger.info("[RTL-SDR diag] rtlsdr_get_device_count() via ctypes = %d", count)
    except Exception as exc:
        logger.warning("[RTL-SDR diag] rtlsdr_get_device_count() call failed: %s", exc)
        return

    if count > 0:
        try:
            get_name = lib.rtlsdr_get_device_name
            get_name.restype = ctypes.c_char_p
            get_name.argtypes = [ctypes.c_uint32]
            name = get_name(0)
            logger.info("[RTL-SDR diag] rtlsdr_get_device_name(0) = %s", name)
        except Exception as exc:
            logger.warning("[RTL-SDR diag] rtlsdr_get_device_name() call failed: %s", exc)

        # NOTE: Do NOT call rtlsdr_open()+rtlsdr_close() here.
        # rtlsdr_close() calls libusb_exit() which resets WinUSB backend state;
        # any subsequent rtlsdr_open() (from SoapyRTLSDR) then fails with
        # "No RTL-SDR devices found!" even though the device is physically present.
        # (Confirmed by v0.1.53 diagnostic: ctypes open succeeded but SoapySDR
        # always failed because our close() broke WinUSB before SoapySDR tried.)


def _soapy_rtlsdr_module_diagnostic(soapy_module: object) -> None:
    """Check whether SoapySDR has the 'rtlsdr' driver registered in the main process.

    If rtlsdrSupport.dll failed to load (e.g. missing dependency), SoapySDR
    won't have the 'rtlsdr' factory registered and will throw "no match" from
    Device::make().  This diagnostic enumerates with driver='rtlsdr' to confirm
    RTL-SDR is visible in the main process.

    IMPORTANT: do NOT call Device.enumerate() with no args here.  An unfiltered
    enumerate uses std::launch::async so SoapySDR spawns a background thread that
    calls rtlsdr_get_device_count() (libusb_init+exit).  Under WinUSB that
    concurrent libusb_exit() corrupts the USB backend state before our main
    Device::make() call can run rtlsdr_open().  Enumerating with driver='rtlsdr'
    uses std::launch::deferred (synchronous) and avoids the race.
    """
    try:

        def _kwargs_str(d: object) -> str:
            """Convert SoapySDRKwargs (SWIG proxy) to a readable string."""
            try:
                return str(soapy_module.KwargsToString(d))  # type: ignore[attr-defined]
            except Exception:
                return str(d)

        # Enumerate with driver filter only (deferred/synchronous — no background thread).
        # This tells us whether rtlsdrSupport.dll was loaded without triggering
        # concurrent libusb_init+exit that would break WinUSB state.
        rtl_results = list(soapy_module.Device.enumerate({"driver": "rtlsdr"}))  # type: ignore[attr-defined]
        rtl_strs = [_kwargs_str(r) for r in rtl_results]
        logger.warning(
            "[RTL-SDR diag] SoapySDR.enumerate(driver=rtlsdr) in main process: %s",
            rtl_strs,
        )
        if not rtl_results:
            logger.warning(
                "[RTL-SDR diag] 'rtlsdr' driver NOT found by SoapySDR in main process! "
                "rtlsdrSupport.dll may have failed to load (missing dependency?)."
            )
    except Exception as exc:
        logger.warning("[RTL-SDR diag] SoapySDR module diagnostic failed: %s", exc)


def _kwargs_to_string(args: dict[str, str]) -> str:
    """Format a SoapySDR args dict as a comma-separated "key=val" string.

    GitHub Issue #12: SoapySDR.Device(dict) marshals a Python dict into a
    C++ Kwargs (std::map<string,string>) through SWIG's multi-step dict
    typemap (calls .items(), converts to a sequence of std::pair, converts
    each pair element). SoapySDR.Device(str) instead marshals a single
    Python str through a plain std::string typemap and lets SoapySDR's own
    C++ KwargsFromString() split it — a much simpler, different code path.
    On the Windows conda-forge build of _SoapySDR.pyd, the dict path was
    confirmed to silently drop the "remote" key's value (host lost, only
    the port survived) while the exact same value round-tripped correctly
    through the string path — reproduced independently on Linux where the
    dict path works fine, isolating this to the Windows binding build.
    SkyRoof (github.com/VE3NEA/SkyRoof), a separate SoapySDR-based Windows
    app, enumerates remote SDRs via the equivalent string-args C API for
    the same "remote=host:port,driver=remote" case, not a struct/dict.
    Only used as an additional fallback attempt — SoapySDR.Device(dict) is
    still the primary, officially documented Python usage.
    """
    return ",".join(f"{k}={v}" for k, v in args.items())


# ---------------------------------------------------------------------------
# RTL-SDR / HackRF ctypes direct devices — duck-type compatible with SoapySDR.Device
#
# On Windows, SoapySDR::Device::make() fails for both RTL-SDR and HackRF with
# "no match" because multiple hackrf_init()+hackrf_exit() / libusb_init()+exit()
# calls during SoapySDR's async enumerate corrupt the WinUSB backend handle cache.
# These classes call librtlsdr.dll / hackrf.dll directly via ctypes, making exactly
# ONE init call at open time and holding the context open until close, so WinUSB
# state is never corrupted.
# ---------------------------------------------------------------------------


class _HackRfTransfer(ctypes.Structure):
    """hackrf_transfer struct layout from libhackrf/hackrf.h (64-bit)."""

    _fields_ = [
        ("device", ctypes.c_void_p),
        ("buffer", ctypes.c_void_p),  # uint8_t* — accessed via ctypes.cast in callback
        ("buffer_length", ctypes.c_int),
        ("valid_length", ctypes.c_int),
        ("rx_ctx", ctypes.c_void_p),
        ("tx_ctx", ctypes.c_void_p),
    ]


_HACKRF_CB_TYPE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(_HackRfTransfer))


class _SrResult:
    """Minimal readStream return value compatible with SoapySDR.StreamResult."""

    __slots__ = ("ret",)

    def __init__(self, ret: int) -> None:
        self.ret = ret


class _RangeResult:
    """Minimal range value compatible with SoapySDR.Range."""

    __slots__ = ("_lo", "_hi")

    def __init__(self, lo: float, hi: float) -> None:
        self._lo = lo
        self._hi = hi

    def minimum(self) -> float:
        return self._lo

    def maximum(self) -> float:
        return self._hi


class RtlSdrDirectDevice:
    """
    ctypes-based RTL-SDR device for Windows, bypassing SoapySDR.Device::make().

    SoapyRTLSDR's C++ constructor succeeds but SoapySDR rejects it at the ABI
    check layer ("no match").  This class calls librtlsdr.dll directly via
    ctypes and exposes a duck-type interface matching SoapySDR.Device so that
    SdrDevice can store it in self._dev without changing any other code path.

    Used only on Windows + driver=="rtlsdr".  All other SDR devices and
    Linux/macOS continue to use the SoapySDR path.
    """

    def __init__(self, device_index: int, lib: ctypes.CDLL) -> None:
        self._dev_index = device_index
        self._lib = lib
        self._handle: ctypes.c_void_p | None = None
        self._setup_cfuncs()

    def _setup_cfuncs(self) -> None:
        lib = self._lib
        lib.rtlsdr_open.restype = ctypes.c_int
        lib.rtlsdr_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32]
        lib.rtlsdr_close.restype = ctypes.c_int
        lib.rtlsdr_close.argtypes = [ctypes.c_void_p]
        lib.rtlsdr_set_center_freq.restype = ctypes.c_int
        lib.rtlsdr_set_center_freq.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.rtlsdr_set_sample_rate.restype = ctypes.c_int
        lib.rtlsdr_set_sample_rate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.rtlsdr_set_tuner_gain_mode.restype = ctypes.c_int
        lib.rtlsdr_set_tuner_gain_mode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.rtlsdr_set_tuner_gain.restype = ctypes.c_int
        lib.rtlsdr_set_tuner_gain.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.rtlsdr_set_freq_correction.restype = ctypes.c_int
        lib.rtlsdr_set_freq_correction.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.rtlsdr_set_bias_tee.restype = ctypes.c_int
        lib.rtlsdr_set_bias_tee.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.rtlsdr_read_sync.restype = ctypes.c_int
        lib.rtlsdr_read_sync.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.rtlsdr_reset_buffer.restype = ctypes.c_int
        lib.rtlsdr_reset_buffer.argtypes = [ctypes.c_void_p]

    def open_device(self) -> bool:
        """Open the RTL-SDR device via rtlsdr_open()."""
        handle = ctypes.c_void_p()
        ret = self._lib.rtlsdr_open(ctypes.byref(handle), ctypes.c_uint32(self._dev_index))
        if ret != 0:
            logger.error("[RTL-SDR direct] rtlsdr_open(index=%d) failed: %d", self._dev_index, ret)
            return False
        self._handle = handle
        self._lib.rtlsdr_reset_buffer(self._handle)
        logger.info(
            "[RTL-SDR direct] rtlsdr_open(index=%d) OK, handle=%s",
            self._dev_index,
            self._handle,
        )
        return True

    def close_device(self) -> None:
        """Close the RTL-SDR device via rtlsdr_close()."""
        if self._handle is not None:
            self._lib.rtlsdr_close(self._handle)
            self._handle = None
            logger.info("[RTL-SDR direct] device closed")

    # -- SoapySDR.Device duck-type interface ----------------------------------

    def setSampleRate(self, direction: int, channel: int, rate: float) -> None:
        if self._handle is not None:
            self._lib.rtlsdr_set_sample_rate(self._handle, ctypes.c_uint32(int(rate)))

    def setFrequency(self, direction: int, channel: int, freq: float) -> None:
        if self._handle is not None:
            self._lib.rtlsdr_set_center_freq(self._handle, ctypes.c_uint32(int(freq)))

    def setBandwidth(self, direction: int, channel: int, bw: float) -> None:
        pass  # RTL-SDR has no programmable IF bandwidth via librtlsdr

    def setGainMode(self, direction: int, channel: int, auto_gain: bool) -> None:
        if self._handle is not None:
            self._lib.rtlsdr_set_tuner_gain_mode(self._handle, 0 if auto_gain else 1)

    def hasGainMode(self, direction: int, channel: int) -> bool:
        return True  # R820T tuner has real hardware AGC (see setGainMode above)

    def setGain(self, direction: int, channel: int, gain_db: float) -> None:
        if self._handle is not None:
            # rtlsdr_set_tuner_gain takes tenths of dB as integer
            self._lib.rtlsdr_set_tuner_gain(self._handle, ctypes.c_int(int(gain_db * 10)))

    def setFrequencyComponent(self, direction: int, channel: int, name: str, value: float) -> None:
        if name == "CORR" and self._handle is not None:
            self._lib.rtlsdr_set_freq_correction(self._handle, ctypes.c_int(int(value)))

    def writeSetting(self, key: str, value: str) -> None:
        if "biastee" in key.lower() and self._handle is not None:
            enabled = value in ("1", "true", "True")
            self._lib.rtlsdr_set_bias_tee(self._handle, 1 if enabled else 0)

    def setupStream(self, direction: int, fmt: str) -> RtlSdrDirectDevice:
        return self  # stream token is self; no setup needed for sync reads

    def activateStream(self, stream: object) -> int:
        return 0

    def deactivateStream(self, stream: object) -> int:
        return 0

    def closeStream(self, stream: object) -> int:
        return 0

    def readStream(
        self,
        stream: object,
        buffers: list[Any],
        numElems: int,
        **kwargs: Any,
    ) -> _SrResult:
        """Read numElems complex64 samples via rtlsdr_read_sync().

        Converts the uint8 interleaved I/Q bytes from librtlsdr into complex64
        by normalising to [-1, +1]: (sample - 127.5) / 127.5.
        """
        if self._handle is None:
            return _SrResult(-1)
        num_bytes = numElems * 2  # each sample = 1 byte I + 1 byte Q
        raw = (ctypes.c_uint8 * num_bytes)()
        n_read = ctypes.c_int(0)
        ret = self._lib.rtlsdr_read_sync(
            self._handle, raw, ctypes.c_int(num_bytes), ctypes.byref(n_read)
        )
        if ret != 0 or n_read.value < 2:
            return _SrResult(-1)
        n_samples = n_read.value // 2
        arr = np.frombuffer(bytes(raw), dtype=np.uint8)[: n_samples * 2].astype(np.float32)
        arr = (arr - 127.5) / 127.5
        buf_cf32 = buffers[0]
        buf_cf32[:n_samples] = arr[0::2] + 1j * arr[1::2]
        return _SrResult(n_samples)

    def getSampleRateRange(self, direction: int, channel: int) -> list[_RangeResult]:
        return [_RangeResult(225e3, 3.2e6)]

    def getGainRange(self, direction: int, channel: int) -> _RangeResult:
        return _RangeResult(0.0, 49.6)


# ---------------------------------------------------------------------------
# HackRF ctypes direct device — duck-type compatible with SoapySDR.Device
# ---------------------------------------------------------------------------


class HackRfDirectDevice:
    """ctypes-based HackRF device for Windows, bypassing SoapySDR.Device::make().

    SoapyHackRF's Device::make() fails on Windows with "no match" because
    multiple hackrf_init()+hackrf_exit() calls during SoapySDR's async enumerate
    corrupt the WinUSB backend handle cache, causing the subsequent hackrf_open()
    to fail with "Could not Open HackRF Device".

    This class calls hackrf.dll directly via ctypes: one hackrf_init() at open
    time, the context held open until close_device(), so WinUSB state is never
    corrupted.  RX uses hackrf_start_rx() with a ctypes CFUNCTYPE callback that
    enqueues numpy chunks; readStream() drains the queue.

    Used only on Windows + driver=="hackrf".  All other SDR devices and
    Linux/macOS continue to use the SoapySDR path.
    """

    def __init__(self, serial: str, lib: ctypes.CDLL) -> None:
        self._serial = serial  # empty string → open first available device
        self._lib = lib
        self._handle: ctypes.c_void_p | None = None
        self._sample_queue: queue.SimpleQueue[np.ndarray] = queue.SimpleQueue()
        self._sample_buf = np.zeros(0, dtype=np.complex64)  # leftover from last chunk
        self._cb_func: Any = None  # MUST keep reference — GC would crash libhackrf
        self._setup_cfuncs()

    def _setup_cfuncs(self) -> None:
        lib = self._lib
        lib.hackrf_init.restype = ctypes.c_int
        lib.hackrf_init.argtypes = []
        lib.hackrf_exit.restype = ctypes.c_int
        lib.hackrf_exit.argtypes = []
        lib.hackrf_open.restype = ctypes.c_int
        lib.hackrf_open.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        lib.hackrf_open_by_serial.restype = ctypes.c_int
        lib.hackrf_open_by_serial.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        lib.hackrf_close.restype = ctypes.c_int
        lib.hackrf_close.argtypes = [ctypes.c_void_p]
        lib.hackrf_set_sample_rate.restype = ctypes.c_int
        lib.hackrf_set_sample_rate.argtypes = [ctypes.c_void_p, ctypes.c_double]
        lib.hackrf_set_freq.restype = ctypes.c_int
        lib.hackrf_set_freq.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        lib.hackrf_set_lna_gain.restype = ctypes.c_int
        lib.hackrf_set_lna_gain.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.hackrf_set_vga_gain.restype = ctypes.c_int
        lib.hackrf_set_vga_gain.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.hackrf_set_amp_enable.restype = ctypes.c_int
        lib.hackrf_set_amp_enable.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
        lib.hackrf_set_antenna_enable.restype = ctypes.c_int  # Bias-T
        lib.hackrf_set_antenna_enable.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
        lib.hackrf_set_baseband_filter_bandwidth.restype = ctypes.c_int
        lib.hackrf_set_baseband_filter_bandwidth.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.hackrf_start_rx.restype = ctypes.c_int
        lib.hackrf_start_rx.argtypes = [ctypes.c_void_p, _HACKRF_CB_TYPE, ctypes.c_void_p]
        lib.hackrf_stop_rx.restype = ctypes.c_int
        lib.hackrf_stop_rx.argtypes = [ctypes.c_void_p]

    def open_device(self) -> bool:
        """Call hackrf_init() once, then hackrf_open[_by_serial]()."""
        ret = self._lib.hackrf_init()
        if ret != 0:
            logger.error("[HackRF direct] hackrf_init() failed: %d", ret)
            return False

        handle = ctypes.c_void_p()
        if self._serial:
            ret = self._lib.hackrf_open_by_serial(
                self._serial.encode("ascii"), ctypes.byref(handle)
            )
        else:
            ret = self._lib.hackrf_open(ctypes.byref(handle))

        if ret != 0:
            logger.error("[HackRF direct] hackrf_open failed: %d (serial=%r)", ret, self._serial)
            self._lib.hackrf_exit()
            return False

        self._handle = handle
        logger.info(
            "[HackRF direct] hackrf_open OK, serial=%r handle=%s", self._serial, self._handle
        )
        return True

    def close_device(self) -> None:
        """Stop RX, close device, and call hackrf_exit() to release libusb context."""
        if self._handle is not None:
            with contextlib.suppress(Exception):
                self._lib.hackrf_stop_rx(self._handle)
            self._lib.hackrf_close(self._handle)
            self._handle = None
        self._cb_func = None  # allow GC after streaming has stopped
        with contextlib.suppress(Exception):
            self._lib.hackrf_exit()
        logger.info("[HackRF direct] device closed")

    # -- SoapySDR.Device duck-type interface ----------------------------------

    def setSampleRate(self, direction: int, channel: int, rate: float) -> None:
        if self._handle is not None:
            ret = self._lib.hackrf_set_sample_rate(self._handle, ctypes.c_double(rate))
            if ret != 0:
                logger.warning("[HackRF direct] hackrf_set_sample_rate(%.0f) = %d", rate, ret)

    def setFrequency(self, direction: int, channel: int, freq: float) -> None:
        if self._handle is not None:
            ret = self._lib.hackrf_set_freq(self._handle, ctypes.c_uint64(int(freq)))
            if ret != 0:
                logger.warning("[HackRF direct] hackrf_set_freq(%d) = %d", int(freq), ret)

    def setBandwidth(self, direction: int, channel: int, bw: float) -> None:
        if self._handle is not None and bw > 0:
            self._lib.hackrf_set_baseband_filter_bandwidth(self._handle, ctypes.c_uint32(int(bw)))

    def setGainMode(self, direction: int, channel: int, auto_gain: bool) -> None:
        # HackRF has no AGC; apply a sensible default when "auto" is requested.
        # SdrDevice normally drives Auto through the software AGC loop
        # instead (see _sw_agc_step()) and calls setGainMode(False), so this
        # branch is a fallback for any caller that still asks for
        # setGainMode(True) directly.
        if auto_gain and self._handle is not None:
            self._lib.hackrf_set_lna_gain(self._handle, ctypes.c_uint32(16))
            self._lib.hackrf_set_vga_gain(self._handle, ctypes.c_uint32(20))

    def hasGainMode(self, direction: int, channel: int) -> bool:
        return False  # HackRF has no hardware AGC (see setGainMode above)

    def setGain(self, direction: int, channel: int, gain_db: float) -> None:
        if self._handle is None:
            return
        # Distribute across LNA (0-40 dB, step 8) and VGA (0-62 dB, step 2).
        lna = min(40, max(0, int(gain_db / 2.0 / 8) * 8))
        vga = min(62, max(0, int((gain_db - lna) / 2) * 2))
        self._lib.hackrf_set_lna_gain(self._handle, ctypes.c_uint32(lna))
        self._lib.hackrf_set_vga_gain(self._handle, ctypes.c_uint32(vga))

    def setFrequencyComponent(self, direction: int, channel: int, name: str, value: float) -> None:
        pass  # HackRF does not support software PPM correction

    def writeSetting(self, key: str, value: str) -> None:
        if self._handle is None:
            return
        if key == "bias_tx":
            enabled = value in ("1", "true", "True")
            self._lib.hackrf_set_antenna_enable(self._handle, ctypes.c_uint8(1 if enabled else 0))

    def setupStream(self, direction: int, fmt: str) -> HackRfDirectDevice:
        return self  # stream token is self; hardware streaming starts in activateStream

    def activateStream(self, stream: object) -> int:
        """Start HackRF RX via hackrf_start_rx() with a ctypes CFUNCTYPE callback."""
        if self._handle is None:
            return -1

        # Flush stale data from a previous run.
        while True:
            try:
                self._sample_queue.get_nowait()
            except queue.Empty:
                break
        self._sample_buf = np.zeros(0, dtype=np.complex64)

        def _rx_cb(transfer_ptr: Any) -> int:
            try:
                t = transfer_ptr.contents
                n_bytes = t.valid_length & ~1  # ensure even (I+Q pairs)
                if n_bytes > 0 and t.buffer:
                    buf_ptr = ctypes.cast(t.buffer, ctypes.POINTER(ctypes.c_uint8))
                    raw = bytes(buf_ptr[:n_bytes])
                    # HackRF delivers signed int8 I/Q; normalise to [-1, +1].
                    arr = np.frombuffer(raw, dtype=np.int8).astype(np.float32)
                    arr /= 128.0
                    cf32 = (arr[0::2] + 1j * arr[1::2]).astype(np.complex64)
                    self._sample_queue.put(cf32)
            except Exception:
                pass
            return 0  # returning non-0 stops streaming

        self._cb_func = _HACKRF_CB_TYPE(_rx_cb)
        ret = self._lib.hackrf_start_rx(self._handle, self._cb_func, None)
        if ret != 0:
            logger.error("[HackRF direct] hackrf_start_rx() failed: %d", ret)
            self._cb_func = None
            return -1
        logger.info("[HackRF direct] RX streaming started")
        return 0

    def deactivateStream(self, stream: object) -> int:
        """Stop HackRF RX streaming."""
        if self._handle is not None:
            ret = self._lib.hackrf_stop_rx(self._handle)
            if ret != 0:
                logger.warning("[HackRF direct] hackrf_stop_rx() = %d", ret)
        self._cb_func = None
        return 0

    def closeStream(self, stream: object) -> int:
        return 0

    def readStream(
        self,
        stream: object,
        buffers: list[Any],
        numElems: int,
        **kwargs: Any,
    ) -> _SrResult:
        """Drain the callback queue and return up to numElems complex64 samples.

        HackRF delivers samples in large async chunks (~65 K samples each).
        An internal leftover buffer accumulates excess samples between calls so
        no data is dropped.  Returns _SrResult(-1) only on actual timeout/error,
        matching the SoapySDR.Device.readStream() convention.
        """
        timeout_s = kwargs.get("timeoutUs", 50_000) / 1_000_000
        buf_cf32 = buffers[0]

        # Serve from leftover buffer first (fast path, no queue access).
        if len(self._sample_buf) >= numElems:
            buf_cf32[:numElems] = self._sample_buf[:numElems]
            self._sample_buf = self._sample_buf[numElems:]
            return _SrResult(numElems)

        # Wait for a new chunk from the libhackrf callback thread.
        try:
            chunk = self._sample_queue.get(timeout=timeout_s)
        except queue.Empty:
            n = len(self._sample_buf)
            if n > 0:
                buf_cf32[:n] = self._sample_buf
                self._sample_buf = np.zeros(0, dtype=np.complex64)
                return _SrResult(n)
            return _SrResult(-1)

        if len(self._sample_buf) > 0:
            chunk = np.concatenate([self._sample_buf, chunk])

        n = min(len(chunk), numElems)
        buf_cf32[:n] = chunk[:n]
        self._sample_buf = chunk[n:] if len(chunk) > n else np.zeros(0, dtype=np.complex64)
        return _SrResult(n)

    def getSampleRateRange(self, direction: int, channel: int) -> list[_RangeResult]:
        return [_RangeResult(2e6, 20e6)]  # HackRF One: 2 MHz – 20 MHz

    def getGainRange(self, direction: int, channel: int) -> _RangeResult:
        return _RangeResult(0.0, 102.0)  # LNA (0-40) + VGA (0-62)


def _distribute_gain(dev: Any, gain_db: float) -> None:
    """Set RX gain, allocating dB across named gain elements ourselves.

    SoapyHackRF's own "overall" gain distribution (dev.setGain(RX, 0, value)
    with no element name) is broken at the top of its declared range: asking
    for the device's own getGainRange() maximum (116 dB) is rejected with a
    C++-side "setGain(...) returned invalid parameter(s)" warning -- no
    Python exception, so it goes unnoticed -- and leaves the gain in a much
    lower, nonsensical state (verified against a real HackRF, 2026-09-11:
    LNA 37 / VGA 65(!) / AMP 14 instead of the correct 40 / 62 / 14 -- VGA
    even reads back above its own declared 62 dB max). This is exactly the
    value "start Auto at the device's real max" (_engage_auto_gain) and "the
    RF Gain spinbox defaults to the device's real max" (rig_dialog.py) both
    ask for, so it can't be sidestepped by just avoiding 116 specifically.
    Filling each named element to its own max before moving to the next
    avoids the driver's overall-gain code path entirely. Devices with no
    named elements (e.g. RTL-SDR, a single overall gain) are unaffected by
    this bug and go through the normal overall call.

    Front-end gain (LNA, then AMP) is filled first since it's generally
    better for noise figure than back-end VGA gain, matching how
    HackRfDirectDevice.setGain() already prioritises this on the Windows
    ctypes bypass path (which never goes through this function at all --
    dev.listGains() isn't defined there, so the no-names branch below calls
    its setGain() directly, and it does its own LNA/VGA split internally).
    """
    try:
        names = list(dev.listGains(_SOAPY_SDR_RX, 0))
    except Exception:
        names = []
    if not names:
        try:
            dev.setGain(_SOAPY_SDR_RX, 0, gain_db)
        except Exception:
            logger.debug("setGain(%.1f) failed", gain_db, exc_info=True)
        return

    def _priority(name: str) -> int:
        n = name.upper()
        if n == "LNA":
            return 0
        if n == "AMP":
            return 1
        return 2

    remaining = gain_db
    for name in sorted(names, key=_priority):
        try:
            rng = dev.getGainRange(_SOAPY_SDR_RX, 0, name)
            lo, hi = float(rng.minimum()), float(rng.maximum())
            step = float(rng.step()) if hasattr(rng, "step") else 0.0
        except Exception:
            continue
        value = max(lo, min(hi, remaining))
        if step > 0:
            value = lo + round((value - lo) / step) * step
            value = max(lo, min(hi, value))
        try:
            dev.setGain(_SOAPY_SDR_RX, 0, name, value)
        except Exception:
            logger.debug("setGain(%s, %.1f) failed", name, value, exc_info=True)
            continue
        remaining -= value


# ---------------------------------------------------------------------------
# Main SdrDevice class
# ---------------------------------------------------------------------------


class SdrDevice:
    """
    Wrapper around a SoapySDR.Device.

    Instantiate with an SdrDeviceInfo (from enumerate()) or a raw kwargs dict.
    Call open() before streaming, close() when done.

    On Windows + driver=="rtlsdr", open() uses RtlSdrDirectDevice (ctypes) instead
    of SoapySDR.Device to bypass the ABI-check "no match" rejection.
    """

    def __init__(self, info: SdrDeviceInfo) -> None:
        if not SOAPY_AVAILABLE:
            raise RuntimeError(
                "SoapySDR is not installed. Install python3-soapysdr to enable SDR support."
            )
        self._info = info
        self._dev: Any = None
        self._stream: Any = None
        self._lock = threading.Lock()
        self._sample_rate: float = 2.4e6
        self._center_freq: float = 435.0e6
        self._bandwidth: float = 0.0  # 0 = auto
        self._gain_mode: str = "auto"  # "auto" or "manual"
        self._gain_db: float = 40.0
        self._ppm: float = 0.0
        self._bias_tee: bool = False
        self._overflow_count: int = 0
        # Software AGC (see _sw_agc_step()): only engaged in "auto" mode for
        # a device whose hasGainMode() says it has no real hardware AGC
        # (e.g. HackRF). _sw_agc_active gates whether read_samples() runs
        # the loop at all; the rest track its running state.
        self._sw_agc_active: bool = False
        self._sw_agc_gain_db: float = 0.0
        self._sw_agc_gain_min: float = 0.0
        self._sw_agc_gain_max: float = 40.0
        self._sw_agc_low_since: float | None = None
        self._sw_agc_last_attack: float = 0.0

    # ------------------------------------------------------------------
    # Class methods
    # ------------------------------------------------------------------

    @classmethod
    def enumerate(cls, force: bool = False) -> list[SdrDeviceInfo]:
        """Return SoapySDR-visible hardware SDR devices (audio devices excluded).

        On Windows, results are cached after the first successful call — pass
        force=True to bypass the cache (e.g. when the user explicitly clicks
        the Enumerate button after plugging in a new device). The cache
        exists only to dodge repeat crash risk from re-invoking Windows'
        out-of-process enumeration subprocess (see below); it must not apply
        on Linux/macOS, where enumeration is a plain in-process SoapySDR
        call with no such risk. Caching there meant a single bad first
        result (e.g. the SOAPY_SDR_PLUGIN_PATH bug fixed in b9e7f1d, or any
        other transient USB hiccup) silently stuck as "no local SDR" for the
        rest of the app's run -- confirmed live: Help > SDR's dialog forces
        a fresh scan on every open and "fixed" it, only because Rig Settings'
        own auto-enumerate-on-open never got a chance to see a good result
        again after caching a bad one.

        On Windows the enumeration runs in a subprocess so that a C-level crash
        inside a SoapySDR plugin (e.g. SoapyRTLSDR with a libusbK driver) cannot
        kill the main Qt process.  On Linux/macOS the direct path is used.
        """
        global _enumerate_cache
        if not SOAPY_AVAILABLE:
            return []
        if sys.platform == "win32":
            if not force and _enumerate_cache is not None:
                return list(_enumerate_cache)
            with _SOAPY_GLOBAL_LOCK:
                results = cls._enumerate_via_subprocess()
                _enumerate_cache = results
                return list(results)
        with _SOAPY_GLOBAL_LOCK:
            return list(cls._enumerate_direct())

    # Local (USB) hardware drivers probed one at a time by _enumerate_direct().
    # We deliberately never call the unfiltered SoapySDR.Device.enumerate():
    # that invokes the SoapyRemote module's findRemote(), which fires an SSDP
    # multicast discovery on every network interface.  On a host with many
    # virtual interfaces (VM bridges, AirDrop awdl0, VPN / iCloud-Private-Relay
    # utun*) that scan stalls for tens of seconds to minutes, and a process
    # killed mid-scan leaks the multicast sockets so every later run is slower
    # still — until only a reboot clears it.  A per-driver enumerate loads only
    # the named driver's module and never touches the network.
    #
    # Trade-off: LAN auto-discovery of SoapyRemote servers is given up.  Remote
    # SDRs are reached exclusively through "Add Remote Host…" (an explicit
    # host:port that SoapyRemote connects to directly, no SSDP).  Network-
    # discovery drivers (uhd, netsdr, rfspace) are also omitted on purpose.
    _LOCAL_SDR_DRIVERS: tuple[str, ...] = (
        "rtlsdr",
        "hackrf",
        "airspy",
        "airspyhf",
        "sdrplay",
        "bladerf",
        "lime",
        "miri",
        "plutosdr",
    )

    @classmethod
    def _enumerate_direct(cls) -> list[SdrDeviceInfo]:
        """Enumerate local SoapySDR hardware in-process (Linux / macOS).

        Probes each known USB driver individually — see _LOCAL_SDR_DRIVERS for
        why the unfiltered SoapySDR.Device.enumerate() must not be used.

        Probes run concurrently (one thread per driver) rather than one
        after another: each call is still filtered to a single named
        driver — no SSDP, no cross-driver shared state — so the only thing
        serial probing bought was making the wall-clock cost the *sum* of
        all nine drivers' individual probe times instead of the slowest
        one. Reported live as "opening SDR Settings takes a long time
        before anything is enumerated" -- worse after the enumerate cache
        (df5ef7d) was restricted to Windows only (9fadf43), since every
        dialog open now pays this cost instead of just the first one per
        process. Results are still merged back in cls._LOCAL_SDR_DRIVERS
        order for a deterministic combo list, regardless of which probe
        thread happens to finish first.
        """
        try:
            import SoapySDR
        except Exception:
            logger.exception("SoapySDR enumerate failed")
            return []

        def _probe(probe_driver: str) -> list[dict[str, str]]:
            try:
                return [dict(kw) for kw in SoapySDR.Device.enumerate({"driver": probe_driver})]
            except Exception:
                logger.debug("enumerate(driver=%s) failed", probe_driver, exc_info=True)
                return []

        with ThreadPoolExecutor(max_workers=len(cls._LOCAL_SDR_DRIVERS)) as pool:
            per_driver = list(pool.map(_probe, cls._LOCAL_SDR_DRIVERS))

        results: list[SdrDeviceInfo] = []
        seen: set[tuple[str, str]] = set()
        for probe_driver, found in zip(cls._LOCAL_SDR_DRIVERS, per_driver, strict=True):
            for d in found:
                driver = str(d.get("driver") or probe_driver)
                if driver.lower() in _NON_SDR_DRIVERS:
                    continue
                label = str(d.get("label") or d.get("device") or driver)
                serial = str(d.get("serial") or "")
                hardware = str(d.get("hardware") or "")
                key = (driver.lower(), serial or label)
                if key in seen:
                    continue
                seen.add(key)
                results.append(
                    SdrDeviceInfo(
                        driver=driver,
                        label=label,
                        serial=serial,
                        hardware=hardware,
                        args=d,
                    )
                )
        return results

    @classmethod
    def query_remote_host(
        cls, host: str, port: str | int = 55132, driver_hint: str = ""
    ) -> list[SdrDeviceInfo]:
        """Enumerate the SDRs on ONE SoapyRemote server, by explicit address.

        Sends a single direct request to ``host:port`` — no SSDP, no LAN-wide
        scan — and returns one SdrDeviceInfo per device the server exposes,
        carrying the real serial / hardware / label forwarded from the remote
        machine.  Returns [] when SoapySDR is unavailable or the host cannot be
        reached.  Used to flesh out a manually added "Add Remote Host…" entry
        so it shows the dongle's serial instead of a blank placeholder.
        """
        if not SOAPY_AVAILABLE or not host:
            return []
        try:
            import SoapySDR
        except Exception:
            return []

        # Fail fast on a genuinely unreachable host (e.g. a saved remote
        # entry for a machine that's since been powered off or
        # decommissioned) instead of paying for name resolution *twice*
        # unbounded -- once in _resolve_ipv4_host() below, once more inside
        # socket.create_connection() itself, since a passed `timeout` only
        # bounds the connect() step, not getaddrinfo(). A dead ".local"
        # mDNS name measured ~5s to fail resolution each time (~10s total)
        # even with an earlier version of this fix that added a bounded
        # socket.create_connection(timeout=2.0) *after* an unbounded
        # _resolve_remote_addr() call. This function is called once per
        # saved remote host on every SDR Settings dialog open (see
        # rig_dialog.py's _start_enumerate()), so one stale host added that
        # same stall to every single open. Reported live: opening SDR
        # Settings taking 10+ seconds even after local USB enumeration
        # itself was sped up (b5c0032).
        #
        # Run the resolve-and-connect probe on a throwaway daemon thread and
        # cap how long *we* wait for it -- Python's blocking socket/DNS
        # calls can't be cancelled once started, so a probe against a truly
        # dead host is simply abandoned (it dies on its own once the OS
        # resolver eventually gives up) rather than making this call wait
        # for it.
        probe_host, _, probe_port_s = host.rpartition(":") if ":" in host else (host, "", "")
        try:
            probe_port = int(probe_port_s) if probe_port_s else int(port)
        except ValueError:
            probe_port = 55132
        reachable_q: queue.Queue[bool] = queue.Queue(maxsize=1)

        def _probe_reachable() -> None:
            try:
                with socket.create_connection((probe_host, probe_port), timeout=2.0):
                    reachable_q.put(True)
            except OSError:
                reachable_q.put(False)

        threading.Thread(target=_probe_reachable, daemon=True).start()
        try:
            reachable = reachable_q.get(timeout=2.5)
        except queue.Empty:
            reachable = False
        if not reachable:
            logger.debug("query_remote_host(%s:%s): unreachable, skipping enumerate", host, port)
            return []

        addr = f"{host}:{port}" if str(port) else str(host)
        addr = _resolve_remote_addr(addr)  # see _resolve_ipv4_host -- fast now, host is reachable

        # String form only: the SWIG dict typemap mangles "remote=host:port"
        # on the macOS conda-forge / Windows builds (GitHub Issue #12).
        query = f"driver=remote,remote={addr}"
        if driver_hint:
            query += f",remote:driver={driver_hint}"
        try:
            with _SOAPY_GLOBAL_LOCK:
                found = list(SoapySDR.Device.enumerate(query))
        except Exception:
            logger.warning("query_remote_host(%s) failed", addr, exc_info=True)
            return []

        results: list[SdrDeviceInfo] = []
        for kw in found:
            d = dict(kw)
            rdrv = str(d.get("remote:driver") or driver_hint or "")
            serial = str(d.get("serial") or "")
            hardware = str(d.get("hardware") or "")
            label = str(d.get("label") or d.get("device") or "").strip()

            # Args we actually open with: the compact "host:port" form (never
            # the server's "tcp://…" echo) plus a driver hint.  A serial is
            # added only when the server exposes more than one device, so the
            # proven single-dongle path keeps its exact 3-key arg set.
            args: dict[str, str] = {"driver": "remote", "remote": addr}
            if rdrv:
                args["remote:driver"] = rdrv
            if serial and len(found) > 1:
                args["serial"] = serial

            # Best-effort: read the device's own real RX gain ceiling so
            # callers (the RF Gain UI) never have to guess one from a
            # hardcoded driver-name table, which cannot know about every
            # SoapySDR-supported device a remote-host user might plug in.
            # enumerate() above only returns identity fields (driver, label,
            # serial, hardware) -- getting the actual gain range requires
            # opening the device, which only makes sense to attempt here,
            # before the caller has committed to a Rig assignment. Any
            # failure (already open elsewhere, driver doesn't support
            # queries this way, network hiccup) just leaves gain_max_db
            # unset; it is not a reason to drop this device from the list.
            gain_max_db: float | None = None
            supports_agc: bool | None = None
            try:
                with _SOAPY_GLOBAL_LOCK:
                    probe_dev = SoapySDR.Device(_kwargs_to_string(args))
                try:
                    gain_max_db = probe_dev.getGainRange(SoapySDR.SOAPY_SDR_RX, 0).maximum()
                    supports_agc = bool(probe_dev.hasGainMode(SoapySDR.SOAPY_SDR_RX, 0))
                finally:
                    # No explicit Device.unmake(): the Python binding's own
                    # __del__ releases the underlying device when the last
                    # reference goes away, and calling unmake() ourselves on
                    # the object make()/Device() already tracks internally
                    # raised "unknown device" here despite closing the
                    # connection correctly -- verified the device is not left
                    # claimed either way. `del` just drops the ref promptly
                    # instead of waiting on function-exit refcounting.
                    del probe_dev
            except Exception:
                logger.debug(
                    "query_remote_host(%s): could not read gain range", addr, exc_info=True
                )

            results.append(
                SdrDeviceInfo(
                    driver="remote",
                    label=label or f"{rdrv or 'remote'} @ {addr}",
                    serial=serial,
                    hardware=hardware,
                    args=args,
                    gain_max_db=gain_max_db,
                    supports_agc=supports_agc,
                )
            )
        return results

    @classmethod
    def _enumerate_via_subprocess(cls) -> list[SdrDeviceInfo]:
        """Enumerate SoapySDR devices in a subprocess (Windows only).

        Spawns the application executable with --_gpredict_soapy_enum so that
        a crash inside a SoapySDR plugin DLL does not kill the main process.
        The worker runs before any Qt/DB init and exits after printing JSON.
        """
        import json as _json
        import subprocess

        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--_gpredict_soapy_enum"]
        else:
            # Dev mode: run main.py with the special argument.
            _main = Path(__file__).parent.parent / "main.py"
            cmd = [sys.executable, str(_main), "--_gpredict_soapy_enum"]

        logger.debug("SoapySDR enumerate: spawning subprocess %s", cmd)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=20,
            )
            stdout = proc.stdout.strip()
            if stdout:
                raw: list[dict[str, str]] = _json.loads(stdout)
                results = cls._parse_raw_devices(raw)
                return cls._win_filter_rtlsdr_by_count(results)
            if proc.returncode != 0:
                logger.warning(
                    "SoapySDR enumerate subprocess exited %d; stderr: %s",
                    proc.returncode,
                    proc.stderr.strip()[:200],
                )
        except Exception:
            logger.exception("SoapySDR enumerate subprocess failed")
        return []

    @classmethod
    def _win_filter_rtlsdr_by_count(cls, results: list[SdrDeviceInfo]) -> list[SdrDeviceInfo]:
        """Filter Windows RTL-SDR enumerate results by actual dongle count via ctypes.

        The patched findRTLSDR (SoapyRTLSDR WinUSB fix) always returns a single
        device_index=0 entry regardless of whether a dongle is plugged in.  Now
        that SoapySDR::Device::make() is bypassed entirely for Windows RTL-SDR,
        calling rtlsdr_get_device_count() here is safe — it runs after the
        subprocess exits (no WinUSB state shared with the subprocess) and before
        any rtlsdr_open() call in the main process.
        """
        rtl_entries = [d for d in results if (d.driver or "").lower() == "rtlsdr"]
        if not rtl_entries:
            return results

        dll_path = _find_rtlsdr_dll()
        if dll_path is None:
            logger.warning("[RTL-SDR enum] rtlsdr.dll not found — cannot verify dongle presence")
            return results

        try:
            lib = ctypes.CDLL(dll_path)
            get_count = lib.rtlsdr_get_device_count
            get_count.restype = ctypes.c_uint32
            get_count.argtypes = []
            real_count = int(get_count())
        except Exception as exc:
            logger.warning("[RTL-SDR enum] rtlsdr_get_device_count() failed: %s", exc)
            return results

        logger.info("[RTL-SDR enum] rtlsdr_get_device_count() = %d", real_count)
        if real_count >= len(rtl_entries):
            return results
        # Keep only as many RTL-SDR entries as physically present (may be 0).
        non_rtl = [d for d in results if (d.driver or "").lower() != "rtlsdr"]
        kept_rtl = rtl_entries[:real_count]
        return non_rtl + kept_rtl

    @classmethod
    def _parse_raw_devices(cls, raw: list[dict[str, str]]) -> list[SdrDeviceInfo]:
        """Convert the JSON dicts from the enumerate worker into SdrDeviceInfo."""
        results: list[SdrDeviceInfo] = []
        for d in raw:
            driver = str(d.get("driver") or "")
            if driver.lower() in _NON_SDR_DRIVERS:
                continue
            label = str(d.get("label") or d.get("device") or driver)
            serial = str(d.get("serial") or "")
            hardware = str(d.get("hardware") or "")
            results.append(
                SdrDeviceInfo(
                    driver=driver,
                    label=label,
                    serial=serial,
                    hardware=hardware,
                    args=d,
                )
            )
        return results

    @classmethod
    def enumerate_usb(cls) -> list[SdrDeviceInfo]:
        """
        Enumerate connected SDR devices via USB VID/PID without SoapySDR.

        Tries pyusb first; falls back to Linux sysfs (/sys/bus/usb/devices/)
        when pyusb is not installed.  Used by the SDR Device Installation
        dialog to identify devices before the driver is installed.
        """
        if PYUSB_AVAILABLE:
            return cls._enumerate_usb_pyusb()
        return cls._enumerate_usb_sysfs()

    @classmethod
    def _enumerate_usb_pyusb(cls) -> list[SdrDeviceInfo]:
        """USB scan via pyusb."""
        try:
            import usb.core

            results: list[SdrDeviceInfo] = []
            for vid, pid, label, module in _KNOWN_USB_DEVICES:
                devs = list(usb.core.find(idVendor=vid, idProduct=pid, find_all=True) or [])
                for _ in devs:
                    results.append(
                        SdrDeviceInfo(
                            driver=None,
                            label=label,
                            serial="",
                            hardware="",
                            vid=vid,
                            pid=pid,
                            soapy_module=module,
                        )
                    )
            return results
        except Exception:
            logger.exception("USB enumeration (pyusb) failed")
            return []

    @classmethod
    def _enumerate_usb_sysfs(cls) -> list[SdrDeviceInfo]:
        """USB scan via Linux sysfs — no extra packages required."""
        import sys

        if sys.platform != "linux":
            return []
        try:
            from pathlib import Path

            known = {(vid, pid): (label, module) for vid, pid, label, module in _KNOWN_USB_DEVICES}
            results: list[SdrDeviceInfo] = []
            sysfs = Path("/sys/bus/usb/devices")
            if not sysfs.exists():
                return []
            for dev_path in sysfs.iterdir():
                vid_file = dev_path / "idVendor"
                pid_file = dev_path / "idProduct"
                if not vid_file.exists() or not pid_file.exists():
                    continue
                try:
                    vid = int(vid_file.read_text().strip(), 16)
                    pid = int(pid_file.read_text().strip(), 16)
                except ValueError:
                    continue
                if (vid, pid) in known:
                    label, module = known[(vid, pid)]
                    results.append(
                        SdrDeviceInfo(
                            driver=None,
                            label=label,
                            serial="",
                            hardware="",
                            vid=vid,
                            pid=pid,
                            soapy_module=module,
                        )
                    )
            return results
        except Exception:
            logger.exception("USB enumeration (sysfs) failed")
            return []

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def info(self) -> SdrDeviceInfo:
        return self._info

    @property
    def is_open(self) -> bool:
        return self._dev is not None

    @property
    def sample_rate(self) -> float:
        return self._sample_rate

    @property
    def overflow_count(self) -> int:
        """Number of SOAPY_SDR_OVERFLOW results seen by read_samples() so far.

        An overflow means the driver's internal ring buffer filled up before
        this process drained it and samples were silently dropped -- any
        throughput-based measurement (e.g. ppm_measure.py) spanning a period
        with overflows is unreliable and should be discarded, not just
        treated as noisy data.
        """
        return self._overflow_count

    @property
    def center_freq(self) -> float:
        return self._center_freq

    # ------------------------------------------------------------------
    # Device lifecycle
    # ------------------------------------------------------------------

    def open(self) -> bool:
        """Open the device. Returns True on success.

        On Windows + driver=="rtlsdr", uses RtlSdrDirectDevice (ctypes) instead of
        SoapySDR.Device to bypass the ABI-check "no match" rejection that occurs
        even when SoapyRTLSDR's C++ constructor succeeds.

        For all other drivers / platforms, uses the SoapySDR path with three arg
        sets per attempt (full / minimal / driver-only) and up to 3 retries.
        """
        import os as _os

        _driver = (self._info.driver or "").lower()
        is_win_rtlsdr = sys.platform == "win32" and _driver == "rtlsdr"
        is_win_hackrf = sys.platform == "win32" and _driver == "hackrf"

        # ── Windows / RTL-SDR diagnostic (always run for visibility) ─────────
        if is_win_rtlsdr:
            _rtlsdr_ctypes_diagnostic()

        # Log all DLLs present in soapy_modules/ so we can detect duplicate
        # plugin files (e.g. two rtlsdrSupport.dll from conda + custom build).
        if sys.platform == "win32":
            _plugin_path = _os.environ.get("SOAPY_SDR_PLUGIN_PATH", "")
            if _plugin_path:
                _dlls = sorted(Path(_plugin_path).glob("*.dll"))
                logger.info("[SDR diag] SOAPY_SDR_PLUGIN_PATH=%s", _plugin_path)
                logger.info("[SDR diag] soapy_modules DLLs: %s", [p.name for p in _dlls])
            else:
                logger.info("[SDR diag] SOAPY_SDR_PLUGIN_PATH is not set")
        # ─────────────────────────────────────────────────────────────────────

        # On Windows, bypass SoapySDR::Device::make() for both RTL-SDR and HackRF.
        # Both fail with "no match" because multiple hackrf_init()/libusb_init()
        # cycles during SoapySDR's async enumerate corrupt the WinUSB handle cache.
        if is_win_rtlsdr:
            return self._open_rtlsdr_direct()
        if is_win_hackrf:
            return self._open_hackrf_direct()

        # ── SoapySDR path (all other drivers / platforms) ─────────────────────
        import SoapySDR

        _MAX_ATTEMPTS = 3
        _RETRY_DELAY = 0.6  # seconds

        # Build fallback arg sets for drivers that fail serial/USB-string matching.
        minimal_args: dict[str, str] = {}
        driver_only_args: dict[str, str] = {}
        if self._info.driver:
            idx = self._info.args.get("device_index", "0")
            minimal_args = {"driver": self._info.driver, "device_index": idx}
            driver_only_args = {"driver": self._info.driver}

        with self._lock:
            if self._dev is not None:
                return True

            # ── SoapySDR log handler: capture C++ error messages ──────────────
            _soapy_log_msgs: list[tuple[int, str]] = []

            def _soapy_log_cb(level: int, msg: str) -> None:
                _soapy_log_msgs.append((level, msg))
                logger.warning("[SoapySDR L%d] %s", level, msg)

            import contextlib as _contextlib

            with _contextlib.suppress(Exception):
                SoapySDR.registerLogHandler(_soapy_log_cb)
            # ──────────────────────────────────────────────────────────────────

            # For SoapyRemote, pass ONLY the full string form.  The SWIG dict
            # typemap on the macOS conda-forge / Windows SoapySDR builds mangles
            # the "remote=host:port" value (GitHub Issue #12): best case
            # Device::make() returns "no match", worst case the host is dropped
            # and SoapyRemote falls back to SSDP auto-discovery, which can hang
            # for minutes on a host with many network interfaces (VM bridges,
            # AirDrop, VPN tunnels).  The string typemap (KwargsFromString) is
            # unaffected.  The minimal / driver-only fallbacks are skipped too:
            # without "remote=" they also trigger the hanging SSDP discovery.
            if (self._info.driver or "").lower() == "remote":
                _remote_args = dict(self._info.args)
                if _remote_args.get("remote"):
                    # Resolve a ".local" host to IPv4 ourselves: SoapyRemote's
                    # C++ client only tries the first getaddrinfo() result and
                    # does not fall back, and on macOS that first result for a
                    # ".local" name is an IPv6 link-local address SoapyRemote
                    # cannot connect to. See _resolve_ipv4_host.
                    _remote_args["remote"] = _resolve_remote_addr(_remote_args["remote"])
                _attempt_specs: list[tuple[str, object]] = [
                    ("full args (string form)", _kwargs_to_string(_remote_args)),
                ]
            else:
                _attempt_specs = [
                    ("full args", self._info.args),
                    ("full args (string form)", _kwargs_to_string(self._info.args)),
                    ("minimal args", minimal_args),
                    ("minimal args (string form)", _kwargs_to_string(minimal_args)),
                    ("driver-only args", driver_only_args),
                    ("driver-only args (string form)", _kwargs_to_string(driver_only_args)),
                ]

            last_exc: Exception | None = None
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                for args_label, args in _attempt_specs:
                    if not args:
                        continue
                    try:
                        with _SOAPY_GLOBAL_LOCK:
                            self._dev = SoapySDR.Device(args)
                        self._apply_settings()
                        logger.info(
                            "SDR opened: %s (attempt %d, %s)",
                            self._info.display_name,
                            attempt,
                            args_label,
                        )
                        return True
                    except Exception as exc:
                        last_exc = exc
                        self._dev = None
                        logger.warning(
                            "SDR open attempt %d/%d (%s) failed for %s: %r",
                            attempt,
                            _MAX_ATTEMPTS,
                            args_label,
                            self._info.display_name,
                            exc,
                        )
                if attempt < _MAX_ATTEMPTS:
                    logger.warning(
                        "SDR open attempt %d/%d failed for %s, retrying in %.1fs…",
                        attempt,
                        _MAX_ATTEMPTS,
                        self._info.display_name,
                        _RETRY_DELAY,
                    )
                    time.sleep(_RETRY_DELAY)
            # Restore default SoapySDR log handler.
            with _contextlib.suppress(Exception):
                SoapySDR.registerLogHandler(None)

            # ── Post-failure SoapySDR module diagnostic (Windows/RTL-SDR) ────
            if sys.platform == "win32" and (self._info.driver or "").lower() == "rtlsdr":
                _soapy_rtlsdr_module_diagnostic(SoapySDR)
            # ──────────────────────────────────────────────────────────────────

            if _soapy_log_msgs:
                logger.warning(
                    "[SoapySDR captured %d log message(s) during open attempts]",
                    len(_soapy_log_msgs),
                )
                for _lvl, _msg in _soapy_log_msgs:
                    logger.warning("[SoapySDR captured msg L%d] %s", _lvl, _msg)
            logger.exception(
                "Failed to open SDR device %s after %d attempts",
                self._info.display_name,
                _MAX_ATTEMPTS,
                exc_info=last_exc,
            )
            return False

    def _open_rtlsdr_direct(self) -> bool:
        """Open RTL-SDR via ctypes on Windows, storing an RtlSdrDirectDevice in self._dev.

        Bypasses SoapySDR::Device::make() which rejects the device at the ABI
        check layer despite SoapyRTLSDR's C++ constructor succeeding.
        """
        dll_path = _find_rtlsdr_dll()
        if dll_path is None:
            logger.error("[RTL-SDR direct] rtlsdr.dll not found — cannot open device")
            return False
        try:
            lib = ctypes.CDLL(dll_path)
        except OSError as exc:
            logger.error("[RTL-SDR direct] Failed to load rtlsdr.dll: %s", exc)
            return False

        dev_index = int(self._info.args.get("device_index", "0"))
        rtldev = RtlSdrDirectDevice(dev_index, lib)
        if not rtldev.open_device():
            return False

        with self._lock:
            self._dev = rtldev
            self._apply_settings()
            logger.info(
                "[RTL-SDR direct] opened device index=%d via ctypes (SoapySDR bypassed)",
                dev_index,
            )
        return True

    def _open_hackrf_direct(self) -> bool:
        """Open HackRF via ctypes on Windows, storing a HackRfDirectDevice in self._dev.

        Bypasses SoapySDR::Device::make() which fails with "no match" on Windows
        because multiple hackrf_init()+hackrf_exit() calls during SoapySDR's async
        enumerate corrupt the WinUSB backend handle cache.
        """
        dll_path = _find_hackrf_dll()
        if dll_path is None:
            logger.error("[HackRF direct] hackrf.dll not found — cannot open device")
            return False
        try:
            lib = ctypes.CDLL(dll_path)
        except OSError as exc:
            logger.error("[HackRF direct] Failed to load hackrf.dll: %s", exc)
            return False

        serial = self._info.serial or ""
        hackrfdev = HackRfDirectDevice(serial, lib)
        if not hackrfdev.open_device():
            return False

        with self._lock:
            self._dev = hackrfdev
            self._apply_settings()
            logger.info("[HackRF direct] opened serial=%r via ctypes (SoapySDR bypassed)", serial)
        return True

    def close(self) -> None:
        """Close the device and release resources."""
        with self._lock:
            self._stop_stream_locked()
            if self._dev is not None:
                if isinstance(self._dev, (RtlSdrDirectDevice, HackRfDirectDevice)):
                    self._dev.close_device()
                self._dev = None
                logger.info("SDR closed: %s", self._info.display_name)

    # ------------------------------------------------------------------
    # Stream control
    # ------------------------------------------------------------------

    def start_stream(self, mtu: int = 1024) -> bool:
        """Activate the RX stream. Returns True on success."""
        import SoapySDR

        with self._lock:
            if self._dev is None:
                return False
            if self._stream is not None:
                return True
            try:
                self._stream = self._dev.setupStream(SoapySDR.SOAPY_SDR_RX, SoapySDR.SOAPY_SDR_CF32)
                self._dev.activateStream(self._stream)
                return True
            except Exception:
                logger.exception("Failed to start SDR stream")
                self._stream = None
                return False

    def stop_stream(self) -> None:
        """Deactivate and close the RX stream."""
        with self._lock:
            self._stop_stream_locked()

    def read_samples(self, num_samples: int = 1024) -> np.ndarray | None:
        """
        Read num_samples complex64 samples.

        Returns None on timeout or error.  Non-blocking: uses a 50 ms timeout
        so the pipeline thread can check a stop flag between reads.

        On the real SoapySDR path, a SOAPY_SDR_OVERFLOW result increments
        self._overflow_count (see the overflow_count property) so callers
        that care about data integrity can detect dropped samples; this
        method's own return value is unaffected (still None, same as any
        other error) to avoid changing behavior for existing callers.  The
        Windows RTL-SDR/HackRF ctypes bypass classes never report this
        distinctly from a generic error, so overflow_count stays 0 there.
        """
        if self._stream is None or self._dev is None:
            return None
        buf = np.zeros(num_samples, dtype=np.complex64)
        try:
            sr = self._dev.readStream(self._stream, [buf], num_samples, timeoutUs=50_000)
            if sr.ret < 0:
                if sr.ret == _SOAPY_SDR_OVERFLOW:
                    self._overflow_count += 1
                return None
            result = buf[: sr.ret] if sr.ret < num_samples else buf
        except Exception:
            return None
        if self._sw_agc_active:
            self._sw_agc_step(result)
        return result

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_sample_rate(self, rate_hz: float) -> bool:
        """Set the ADC sample rate in Hz."""
        with self._lock:
            self._sample_rate = rate_hz
            if self._dev is None:
                return True
            try:
                self._dev.setSampleRate(0, 0, rate_hz)  # direction=RX, channel=0
                return True
            except Exception:
                logger.exception("set_sample_rate failed")
                return False

    def set_center_freq(self, freq_hz: float) -> bool:
        """Tune the center frequency in Hz."""
        import SoapySDR

        with self._lock:
            self._center_freq = freq_hz
            if self._dev is None:
                return True
            try:
                self._dev.setFrequency(SoapySDR.SOAPY_SDR_RX, 0, freq_hz)
                return True
            except Exception:
                logger.exception("set_center_freq failed")
                return False

    def set_bandwidth(self, bw_hz: float) -> bool:
        """Set the IF bandwidth in Hz (0 = automatic)."""
        with self._lock:
            self._bandwidth = bw_hz
            if self._dev is None:
                return True
            if bw_hz <= 0:
                return True
            try:
                self._dev.setBandwidth(0, 0, bw_hz)
                return True
            except Exception:
                logger.exception("set_bandwidth failed")
                return False

    def set_gain_auto(self) -> bool:
        """Enable automatic gain control.

        Uses the device's own hardware AGC (setGainMode) when it has one.
        For a device that doesn't (HackRF -- hasGainMode() is False, and
        setGainMode(True) is a silent no-op that leaves the real gain
        undefined) this falls back to a software AGC loop driven from
        read_samples() instead -- see _sw_agc_step().
        """
        with self._lock:
            self._gain_mode = "auto"
            if self._dev is None:
                return True
            return self._engage_auto_gain()

    def set_gain_db(self, gain_db: float) -> bool:
        """Set manual gain in dB."""
        with self._lock:
            self._gain_mode = "manual"
            self._gain_db = gain_db
            self._sw_agc_active = False
            if self._dev is None:
                return True
            try:
                self._dev.setGainMode(_SOAPY_SDR_RX, 0, False)
                _distribute_gain(self._dev, gain_db)
                return True
            except Exception:
                logger.exception("set_gain_db failed")
                return False

    def _engage_auto_gain(self) -> bool:
        """Turn on Auto gain on self._dev (real AGC or the software fallback).

        Callable with or without self._lock held -- it only touches
        self._dev and the _sw_agc_* fields, never re-enters the lock.
        """
        if self._dev is None:
            return True
        try:
            hw_agc = bool(self._dev.hasGainMode(_SOAPY_SDR_RX, 0))
        except Exception:
            hw_agc = False
        try:
            if hw_agc:
                self._dev.setGainMode(_SOAPY_SDR_RX, 0, True)
                self._sw_agc_active = False
            else:
                # Start at the device's real max: right for the common case
                # of a weak satellite signal, and _sw_agc_step() backs off
                # within the first chunk or two if the signal is actually
                # strong enough to saturate the ADC (e.g. a local FM
                # broadcast station -- see the module-level AGC constants).
                self._dev.setGainMode(_SOAPY_SDR_RX, 0, False)
                gmin, gmax = self.get_gain_range()
                self._sw_agc_gain_min = gmin
                self._sw_agc_gain_max = gmax
                self._sw_agc_gain_db = gmax
                self._sw_agc_low_since = None
                self._sw_agc_last_attack = 0.0
                _distribute_gain(self._dev, gmax)
                self._sw_agc_active = True
            return True
        except Exception:
            logger.exception("set_gain_auto failed")
            return False

    def _sw_agc_step(self, iq: np.ndarray) -> None:
        """Adjust gain for a device with no real hardware AGC.

        Called from read_samples() on every chunk while _sw_agc_active.
        Attack: while the peak is above target, back off one step at a time,
        rate-limited by _SW_AGC_ATTACK_COOLDOWN_S -- a single read_samples()
        call only returns one network datagram's worth of samples (a few
        hundred microseconds), and a step's effect takes a few of those to
        reach the stream, so reacting to every chunk without a cooldown
        overshoots (ratchets down far past the gain actually needed before
        the first step is even visible -- verified against a real HackRF).
        Release: only creep the gain back up after a sustained quiet period,
        so a brief fade doesn't trigger a hike that immediately re-clips.
        """
        if len(iq) == 0:
            return
        dev = self._dev
        if dev is None:
            return
        peak = float(np.max(np.abs(iq)))
        now = time.monotonic()
        if peak > _SW_AGC_TARGET_PEAK:
            self._sw_agc_low_since = None
            if now - self._sw_agc_last_attack < _SW_AGC_ATTACK_COOLDOWN_S:
                return  # let the last step's effect reach the sample stream first
            new_gain = max(self._sw_agc_gain_min, self._sw_agc_gain_db - _SW_AGC_STEP_DB)
            self._sw_agc_apply(dev, new_gain)
            self._sw_agc_last_attack = now
            return
        if peak < _SW_AGC_LOW_PEAK:
            if self._sw_agc_low_since is None:
                self._sw_agc_low_since = now
            elif now - self._sw_agc_low_since >= _SW_AGC_RELEASE_S:
                new_gain = min(self._sw_agc_gain_max, self._sw_agc_gain_db + _SW_AGC_STEP_DB)
                self._sw_agc_apply(dev, new_gain)
                self._sw_agc_low_since = now
        else:
            self._sw_agc_low_since = None

    def _sw_agc_apply(self, dev: Any, gain_db: float) -> None:
        if gain_db == self._sw_agc_gain_db:
            return
        try:
            _distribute_gain(dev, gain_db)
            self._sw_agc_gain_db = gain_db
        except Exception:
            logger.debug("software AGC setGain(%.1f) failed", gain_db, exc_info=True)

    def set_bias_tee(self, enabled: bool) -> bool:
        """Enable or disable the Bias-T power supply on the antenna port.

        Bias-T injects DC voltage into the coax to power an external LNA or
        active antenna.  Unknown writeSetting keys are silently ignored by
        SoapySDR — trying keys in order until "no exception" does NOT work.
        We must select the correct key based on the driver name.

          Driver     Key        Values
          hackrf     bias_tx    "true" / "false"
          rtlsdr     biastee    "1" / "0"
          airspy     biastee    "true" / "false"
          (others)   biastee    "true" / "false"  (best-effort)
        """
        with self._lock:
            self._bias_tee = enabled
            if self._dev is None:
                return True

            driver = (self._info.driver or "").lower()

            if "hackrf" in driver:
                key = "bias_tx"
                value = "true" if enabled else "false"
            elif "rtlsdr" in driver or "rtl" in driver:
                key = "biastee"
                value = "1" if enabled else "0"
            else:
                key = "biastee"
                value = "true" if enabled else "false"

            try:
                self._dev.writeSetting(key, value)
                logger.info(
                    "Bias-T %s (driver='%s', key='%s', value='%s')",
                    "ON" if enabled else "OFF",
                    driver,
                    key,
                    value,
                )
                return True
            except Exception:
                logger.warning("Bias-T writeSetting failed (driver='%s', key='%s')", driver, key)
                return False

    def set_ppm(self, ppm: float) -> bool:
        """Set frequency correction in parts per million."""
        import SoapySDR

        with self._lock:
            self._ppm = ppm
            if self._dev is None:
                return True
            try:
                self._dev.setFrequencyComponent(SoapySDR.SOAPY_SDR_RX, 0, "CORR", ppm)
                return True
            except Exception:
                # Not all drivers support PPM correction via this call
                return False

    def get_sample_rates(self) -> list[float]:
        """Return list of supported sample rates (Hz)."""
        if not SOAPY_AVAILABLE or self._dev is None:
            return [250e3, 1.0e6, 1.4e6, 1.8e6, 2.0e6, 2.4e6, 3.2e6]
        try:
            ranges = self._dev.getSampleRateRange(0, 0)
            # Return a curated set within the supported range
            candidates = [250e3, 500e3, 1.0e6, 1.4e6, 1.8e6, 2.0e6, 2.4e6, 3.2e6]
            lo = ranges[0].minimum() if ranges else 0
            hi = ranges[0].maximum() if ranges else 4e6
            return [r for r in candidates if lo <= r <= hi]
        except Exception:
            return [250e3, 1.0e6, 2.4e6]

    def get_gain_range(self) -> tuple[float, float]:
        """Return (min_db, max_db) for the overall RX gain element."""
        if not SOAPY_AVAILABLE or self._dev is None:
            return (0.0, 50.0)
        try:
            r = self._dev.getGainRange(_SOAPY_SDR_RX, 0)
            return (r.minimum(), r.maximum())
        except Exception:
            return (0.0, 50.0)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_settings(self) -> None:
        """Push stored settings to the freshly opened device.

        Each setting is applied independently; failures are logged as warnings
        rather than raised so that one unsupported setting does not prevent the
        device from opening (e.g. RTL-SDR ignoring bandwidth setting).
        """
        import SoapySDR

        if self._dev is None:
            return
        try:
            self._dev.setSampleRate(SoapySDR.SOAPY_SDR_RX, 0, self._sample_rate)
        except Exception as exc:
            logger.warning("setSampleRate failed: %s", exc)
        try:
            self._dev.setFrequency(SoapySDR.SOAPY_SDR_RX, 0, self._center_freq)
        except Exception as exc:
            logger.warning("setFrequency failed: %s", exc)
        if self._bandwidth > 0:
            with contextlib.suppress(Exception):
                self._dev.setBandwidth(SoapySDR.SOAPY_SDR_RX, 0, self._bandwidth)
        try:
            if self._gain_mode == "auto":
                self._engage_auto_gain()
            else:
                self._dev.setGainMode(SoapySDR.SOAPY_SDR_RX, 0, False)
                _distribute_gain(self._dev, self._gain_db)
                self._sw_agc_active = False
        except Exception as exc:
            logger.warning("setGain/GainMode failed: %s", exc)
        if self._ppm != 0.0:
            with contextlib.suppress(Exception):
                self._dev.setFrequencyComponent(SoapySDR.SOAPY_SDR_RX, 0, "CORR", self._ppm)
        if self._bias_tee:
            # Use driver-aware key selection (same logic as set_bias_tee)
            driver = (self._info.driver or "").lower()
            if "hackrf" in driver:
                bias_key, bias_val = "bias_tx", "true"
            elif "rtlsdr" in driver or "rtl" in driver:
                bias_key, bias_val = "biastee", "1"
            else:
                bias_key, bias_val = "biastee", "true"
            with contextlib.suppress(Exception):
                self._dev.writeSetting(bias_key, bias_val)

    def _stop_stream_locked(self) -> None:
        """Stop and release the stream. Must be called with _lock held."""
        if self._stream is not None and self._dev is not None:
            try:
                self._dev.deactivateStream(self._stream)
                self._dev.closeStream(self._stream)
            except Exception:
                pass
            self._stream = None


import contextlib  # noqa: E402  (placed here to avoid top-level cycle)
